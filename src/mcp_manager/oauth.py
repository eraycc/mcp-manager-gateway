"""OAuth authorization code with PKCE and isolation-aware sealed credentials."""
import asyncio
import base64
import copy
import hashlib
import json
import math
import secrets
import time
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute

from .credentials import config_isolation, credential_owner
from .database import McpServer, SystemSetting
from .identity import current_user, revalidate_user
from .oauth_states import STATE_TTL, OAuthFlows, state_error
from .runtime import GatewayError
from .transports import check_url


class OAuthCallbackAuditRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        if self.path not in {"/api/v1/oauth/callback", "/callback"}:
            return handler

        async def audited(request):
            try:
                return await handler(request)
            except HTTPException:
                if not getattr(request.state, "oauth_callback_failure_audited", False):
                    state = request.query_params.get("state", "")
                    await callback_failure(request, state, "state_missing" if not state else "state_mismatch")
                raise

        return audited


router = APIRouter(prefix="/api/v1", route_class=OAuthCallbackAuditRoute)
public_router = APIRouter(route_class=OAuthCallbackAuditRoute)
TOKEN_REQUEST_TIMEOUT = 30
CIMD_REJECTIONS = {"invalid_client", "invalid_client_metadata", "unauthorized_client"}


def token_expiry(value: dict) -> float:
    """JSON null means no expiry; accept historical sealed positive Infinity."""
    expiry = value.get("expires_at")
    if expiry is None:
        return float("inf")
    try:
        number = float(expiry)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if not math.isnan(number) else 0.0


def token_from_auth(auth: dict) -> dict:
    value = {
        key: auth[key]
        for key in (
            "access_token",
            "refresh_token",
            "expires_at",
            "expires_in",
            "id_token",
            "token_type",
        )
        if key in auth
    }
    if "granted_scope" in auth:
        value["scope"] = auth["granted_scope"]
    return value


def configuration_fingerprint(auth: dict) -> str:
    excluded = {
        "access_token",
        "refresh_token",
        "expires_at",
        "expires_in",
        "granted_scope",
        "id_token",
        "token_type",
    }
    value = {key: item for key, item in auth.items() if key not in excluded}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def oauth_auth_status(payload):
    token = payload.get("oauth_token", {})
    if payload.get("oauth_state", {}).get("status") == "needs_reauth" or not token.get("access_token"):
        return "needs_reauth"
    return "valid" if token_expiry(token) > time.time() + 60 else "expired"


def token_http_client():
    from .oauth_http import OAuthHTTPTransport

    return httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False, transport=OAuthHTTPTransport())


class OAuth:
    def __init__(self, catalog, credentials=None):
        self.catalog = catalog
        self.credential_store = credentials
        self.flows = OAuthFlows()
        self.pending = self.flows.pending
        self.public_url_binding = catalog.config.public_url if catalog else None
        self.locks = {}
        self.refresh_serials = {}
        from .oauth_metadata import CIMDVerifier

        self.cimd = CIMDVerifier(catalog)

    async def registration_owner(self, row, user_id, *, session=None):
        owner = self.config_owner(row, user_id)
        token_owner = credential_owner(row.isolation, user_id)
        if owner != token_owner:
            payload = await self.credential_store.load(row.id, token_owner, session=session)
            if payload.get("oauth_registration_private"):
                return token_owner
        return owner

    def client_binding(self, auth, effective, resource):
        from .oauth_config import TOKEN_FIELDS, callback_uri, registration_fingerprint

        return {
            "config_fingerprint": registration_fingerprint(
                auth, resource, callback_uri(self.catalog.config.public_url, auth)),
            "resource": resource, "mode": effective.get("registration_mode", "manual"),
            "auth": {key: copy.deepcopy(value) for key, value in effective.items() if key not in TOKEN_FIELDS},
        }

    async def sync_public_url(self, user_id=None):
        if self.public_url_binding == self.catalog.config.public_url:
            return
        records = []
        async with self.catalog.db.locked():
            if self.public_url_binding != self.catalog.config.public_url:
                identities = {key[0] for key in self.flows.generations}
                identities.update(item["server_id"] for item in self.pending.values())
                self.public_url_binding = self.catalog.config.public_url
                records = [(identity, self.flows.invalidate(identity)) for identity in identities]
        for identity, count in records:
            await self.audit("oauth.state_invalidated", user_id, identity,
                             reason="config_changed", cleared_states=count)

    def key(self, row, user_id):
        """Return the legacy key retained for compatibility reads."""
        if not user_id:
            raise GatewayError(
                "auth_required", "Personal OAuth requires a signed-in user"
            )
        return "oauth:" + row.id + ":" + str(user_id)

    async def audit(self, action, user_id, server_id, **details):
        await self.catalog.logs.audit(action, user_id, {"mcp_id": server_id, **details})

    async def validate_access(self, session, row, user_id):
        """Recheck user scope around refresh network I/O, including shared tokens."""
        if user_id:
            from .database import User

            user = await session.get(User, user_id, populate_existing=True)
            if not user or user.disabled:
                raise GatewayError("auth_required", "Sign in with an active user before using OAuth")
            if user.role != "admin" and user.scope_mode != "all" and row.id not in user.mcp_ids:
                raise GatewayError("access_denied", "MCP access revoked")

    async def mark_reauth(self, row, owner, expected, reason="token_expired", *, epoch=None):
        async with self.catalog.db.locked() as session:
            server = await session.get(McpServer, row.id)
            payload = await self.credential_store.load(row.id, owner, session=session)
            if (not server or server.revision != row.revision or payload.get("oauth_token") != expected
                    or epoch is not None and payload.get("oauth_epoch", 0) != epoch):
                return False
            before = await self.catalog._index_scope(
                session, server, owner if row.isolation == "user" else "service")
            payload["oauth_epoch"] = payload.get("oauth_epoch", 0) + 1
            payload["oauth_state"] = {"status": "needs_reauth", "reason": reason, "at": time.time()}
            await self.credential_store.write(session, row.id, owner, payload)
            self.catalog.runtime.credential_versions[(row.id, owner)] = "oauth-needs-reauth"
            await session.flush()
            after = await self.catalog._index_scope(
                session, server, owner if row.isolation == "user" else "service")
            await self.catalog.retain_renewed_catalog(session, before, after)
        return True

    async def record_reauth_index(self, scope):
        try:
            await self.catalog.index.record_failure(
                scope, {"cache_error": "OAuth authorization changed; authorize again",
                        "cache_error_code": "auth_required"},
                expected_refresh=await self.catalog.index.refresh_version(scope))
        except GatewayError as exc:
            if exc.code not in {"disabled", "revision_changed", "credential_changed"}:
                raise

    async def mark_reauth_spec(self, spec):
        if spec.config.get("auth", {}).get("type") != "oauth":
            return
        try:
            row = await self.catalog.get(spec.id)
        except HTTPException as exc:
            if exc.status_code == 404:
                return
            raise
        if row.revision == spec.revision:
            await self.mark_reauth(row, spec.credential_owner, token_from_auth(spec.config["auth"]), "http_401",
                                   epoch=spec.credential_epoch)

    def resource(self, row):
        from .environment import connection_config

        config = self.catalog.unseal(row.config)
        resource = connection_config({key: config[key] for key in ("url", "environment_expansion") if key in config}).get("url", "")
        if resource and row.transport in {"streamable-http", "sse"}:
            from .oauth_discovery import canonical_resource

            return canonical_resource(resource)
        return resource

    async def credentials(self, row, user_id, *, with_epoch=False, force_refresh=False):
        owner = credential_owner(row.isolation, user_id)
        key = self.credential_store.key(row.id, owner)
        refresh_serial = self.refresh_serials.get(key, 0)
        async with self.locks.setdefault(key, asyncio.Lock()):
            async with self.catalog.db.locked() as session:
                config = await self.credential_store.materialize(row, user_id, session=session)
                payload = await self.credential_store.load(row.id, owner, session=session)
            auth = config.get("auth", {})
            value = token_from_auth(auth)
            epoch = payload.get("oauth_epoch", 0)
            if payload.get("oauth_state", {}).get("status") == "needs_reauth":
                raise GatewayError("auth_required", "授权已失效，请点击重新授权以继续使用")
            if not value.get("access_token"):
                raise GatewayError("auth_required", "Complete OAuth authorization in the Web console")
            registration = (await self.credential_store.load(
                row.id, await self.registration_owner(row, user_id))).get("oauth_registration")
            if registration or payload.get("oauth_token_client"):
                try:
                    await self.effective_auth(row, user_id, auth)
                except GatewayError as exc:
                    reason = "binding_changed" if exc.code == "auth_required" else "client_registration_invalid"
                    await self.mark_reauth(row, owner, value, reason, epoch=epoch)
                    raise GatewayError("auth_required", str(exc)) from exc
            force_current = force_refresh and refresh_serial == self.refresh_serials.get(key, 0)
            if not force_current and token_expiry(value) > time.time() + 60:
                return (value, epoch) if with_epoch else value
            if not value.get("refresh_token"):
                if token_expiry(value) > time.time() + 60:
                    raise GatewayError("oauth_refresh_unavailable", "This authorization has no refresh token")
                await self.mark_reauth(row, owner, value, epoch=epoch)
                raise GatewayError("auth_required", "授权已过期，请点击重新授权以继续使用")

            async with self.catalog.db.locked() as session:
                await self.validate_access(session, row, user_id)
            resource_binding = self.resource(row)
            callback_binding = self.catalog.config.public_url
            try:
                effective = await self.effective_auth(row, user_id, auth)
                refreshed = value | await self.exchange(effective, {
                    "grant_type": "refresh_token", "refresh_token": value["refresh_token"],
                })
            except (GatewayError, httpx.HTTPError, ValueError) as exc:
                await self.mark_reauth(row, owner, value, "refresh_failed", epoch=epoch)
                await self.audit("oauth.refresh", user_id, row.id, result="needs_reauth")
                raise GatewayError("auth_required", "自动刷新未成功，请重新授权") from exc
            async with self.catalog.db.locked() as session:
                await self.validate_access(session, row, user_id)
                current = await self.credential_store.load(row.id, owner, session=session)
                server = await session.get(McpServer, row.id)
                if (current.get("oauth_token") != value or not server or server.revision != row.revision
                        or current.get("oauth_epoch", 0) != epoch
                        or current.get("oauth_state", {}).get("status") == "needs_reauth"
                        or self.resource(server) != resource_binding or self.catalog.config.public_url != callback_binding):
                    raise GatewayError("auth_required", "OAuth authorization changed during refresh")
                await self.credential_store.save_oauth_token(
                    session, server, user_id, refreshed, client=self.client_binding(auth, effective, resource_binding),
                    renewal=True)
                epoch += 1
                self.catalog.runtime.credential_versions[(row.id, owner)] = None
            self.refresh_serials[key] = self.refresh_serials.get(key, 0) + 1
            await self.catalog.runtime.invalidate_credential(row.id, owner)
            await self.audit("oauth.refresh", user_id, row.id, result="success")
            return (refreshed, epoch) if with_epoch else refreshed

    def config_owner(self, row, user_id):
        return credential_owner(row.isolation, user_id) if row.isolation == "user" and row.config_isolation == "user" else "service"

    async def effective_auth(self, row, user_id, auth):
        from .oauth_config import callback_uri, registration_matches, registration_policy

        token_payload = await self.credential_store.load(row.id, credential_owner(row.isolation, user_id))
        payload = await self.credential_store.load(row.id, await self.registration_owner(row, user_id))
        registration = token_payload.get("oauth_token_client") or payload.get("oauth_registration", {})
        resource = self.resource(row)
        if registration_matches(registration, auth, resource, callback_uri(self.catalog.config.public_url, auth)):
            from .oauth_discovery import auth_method

            effective = auth | registration["auth"]
            auth_method(effective, effective)
            return effective
        if registration or registration_policy(auth) != "pre_registration" or not all(auth.get(key) for key in ("authorization_url", "token_url")):
            raise GatewayError("auth_required", "OAuth 客户端信息需要重新授权，请在控制台点击重新授权")
        return auth

    async def prepare(self, row, user_id, auth, request, epoch, *, fallback_reason=None, flow_generation=None):
        from .oauth_config import (
            allows_dcr,
            callback_uri,
            client_metadata,
            registration_fingerprint,
            registration_matches,
            registration_policy,
        )
        from .oauth_discovery import auth_method, canonical_resource, discover, register, registration_method

        config_owner = await self.registration_owner(row, user_id)
        token_owner = credential_owner(row.isolation, user_id)
        registration_epoch = self.flows.registration_epochs.get((row.id, token_owner), 0)
        lock = "registration:" + self.credential_store.key(row.id, config_owner)
        resource = self.resource(row)
        redirect_uri = callback_uri(self.catalog.config.public_url, auth)
        fingerprint = registration_fingerprint(auth, resource, redirect_uri)
        async with self.locks.setdefault(lock, asyncio.Lock()):
            payload = await self.credential_store.load(row.id, config_owner)
            cached = payload.get("oauth_registration", {})
            mode = registration_policy(auth)
            if mode == "pre_registration" and all(auth.get(key) for key in ("authorization_url", "token_url")):
                context = {key: auth[key] for key in ("authorization_url", "token_url", "issuer_url", "scopes") if key in auth}
                if resource and row.transport in {"streamable-http", "sse"}:
                    context["resource"] = canonical_resource(resource)
            else:
                context = await discover(resource, auth)
            async with self.catalog.db.locked() as session:
                _, _, current_auth = await oauth_actor(request, session, row.id, revision=row.revision)
                if flow_generation is not None:
                    self.flows.check(row.id, token_owner, flow_generation)
                current_token = await self.credential_store.load(row.id, token_owner, session=session)
                if (configuration_fingerprint(current_auth) != configuration_fingerprint(auth)
                        or current_token.get("oauth_epoch", 0) != epoch or self.resource(row) != resource
                        or callback_uri(self.catalog.config.public_url, current_auth) != redirect_uri):
                    raise HTTPException(409, "OAuth authorization changed during discovery")
            registration_mode = "manual"
            identity = auth.get("client_id", "")
            if mode != "pre_registration":
                reason = fallback_reason
                fallback_code = fallback_reason
                if mode != "dcr_only" and not reason:
                    try:
                        if not context.get("cimd_supported"):
                            reason = "cimd_not_supported"
                            raise GatewayError("oauth_configuration_required", "服务器不支持 CIMD")
                        reason = "public_url_unavailable"
                        identity = (await self.cimd.ensure(request, user_id, auth)
                                    if auth.get("cimd_metadata_source", "mmg") == "external"
                                    else client_metadata(self.catalog.config.public_url)["client_id"])
                        reason = "token_auth_incompatible"
                        if auth.get("token_endpoint_auth_method") not in {None, "none"}:
                            raise GatewayError("oauth_configuration_required",
                                "CIMD 当前使用公开客户端 none，不能使用所选 Secret 认证方式")
                        method = auth_method({"token_endpoint_auth_method": "none"}, context)
                    except GatewayError as exc:
                        if mode == "cimd_only":
                            raise
                        reason = reason or exc.code
                        fallback_code = exc.code
                    else:
                        context["token_endpoint_auth_method"] = method
                        registration_mode = "cimd"
                if registration_mode != "cimd":
                    if not allows_dcr(auth):
                        raise GatewayError("oauth_configuration_required", "所选注册方式不允许 DCR 回退")
                    if reason:
                        await self.audit("oauth.cimd_fallback", user_id, row.id,
                                         reason=reason, error_code=fallback_code, stage="authorization" if fallback_reason else "prerequisite",
                                         result="dcr" if context.get("registration_url") else "unavailable")
                    if not context.get("registration_url"):
                        message = ("CIMD 不可用，且服务器未提供动态注册端点，请手动填写 Client ID（预注册）。"
                                   if mode != "dcr_only" else
                                   "服务器未提供动态注册端点，请手动填写 Client ID（预注册）。")
                        raise GatewayError("oauth_configuration_required", message)
                    registration_mode = "dcr"
                    requested = registration_method(auth, context)
                    registered = None
                    if (registration_matches(cached, auth, resource, redirect_uri) and cached.get("mode") == "dcr"
                            and cached.get("auth", {}).get("issuer_url") == context.get("issuer_url")
                            and cached.get("auth", {}).get("registration_url") == context.get("registration_url")):
                        candidate = cached.get("auth", {})
                        try:
                            method = auth_method(candidate, context)
                            if candidate.get("client_id") and (auth.get("token_endpoint_auth_method") is None or requested == method):
                                registered = {key: candidate[key] for key in (
                                    "client_id", "client_secret", "client_secret_expires_at", "token_endpoint_auth_method",
                                ) if key in candidate}
                        except GatewayError:
                            pass  # Only an explicit authorization start may replace unusable registration.
                    if registered is None:
                        registered = await register(context | {
                            "token_endpoint_auth_method": requested,
                            "token_auth_method_explicit": auth.get("token_endpoint_auth_method") is not None,
                        }, auth.get("client_name", "Codex"), redirect_uri)
                    context.update(registered)
                    identity = registered["client_id"]
            if mode != "pre_registration" and not identity:
                raise GatewayError("oauth_configuration_required", "OAuth Client ID is unavailable")
            context["client_id"] = identity
            context["token_endpoint_auth_method"] = auth_method(auth if mode == "pre_registration" else context, context)
            context["registration_mode"] = registration_mode
            effective = auth | context
            invalidated = set()
            async with self.catalog.db.locked() as session:
                _, current, current_auth = await oauth_actor(request, session, row.id, revision=row.revision)
                # A newer start may reuse this client; reset/config changes must prevent republishing it.
                if self.flows.registration_epochs.get((row.id, token_owner), 0) != registration_epoch:
                    raise state_error("state_mismatch", 409)
                token_payload = await self.credential_store.load(row.id, token_owner, session=session)
                if (configuration_fingerprint(current_auth) != configuration_fingerprint(auth)
                        or token_payload.get("oauth_epoch", 0) != epoch or self.resource(current) != resource
                        or callback_uri(self.catalog.config.public_url, current_auth) != redirect_uri):
                    raise HTTPException(409, "OAuth configuration or authorization changed; restart authorization")
                current_payload = await self.credential_store.load(row.id, config_owner, session=session)
                previous = current_payload.get("oauth_registration", {})
                previous_auth = previous.get("auth", {})
                issuer_changed = bool(previous_auth.get("issuer_url")) and previous_auth["issuer_url"] != context.get("issuer_url")
                client_changed = bool(previous_auth.get("client_id")) and any(
                    previous_auth.get(key) != context.get(key)
                    for key in ("client_id", "client_secret", "token_endpoint_auth_method"))
                if issuer_changed or client_changed:
                    reason = "issuer_changed" if issuer_changed else "client_changed"
                    owners = await self.credential_store.owners(row.id, session=session) if config_owner == "service" else {token_owner}
                    for owner in owners:
                        item = await self.credential_store.load(row.id, owner, session=session)
                        bound_client = item.get("oauth_token_client", {}).get("auth", {})
                        if (not issuer_changed and config_owner == "service"
                                and item.get("oauth_registration_private") and bound_client
                                and any(bound_client.get(key) != previous_auth.get(key)
                                        for key in ("client_id", "client_secret", "token_endpoint_auth_method"))):
                            continue
                        item.pop("oauth_token", None)
                        item.pop("oauth_token_client", None)
                        item["oauth_epoch"] = item.get("oauth_epoch", 0) + 1
                        item["oauth_state"] = {"status": "needs_reauth", "reason": reason, "at": time.time()}
                        await self.credential_store.write(session, row.id, owner, item)
                        self.catalog.runtime.credential_versions[(row.id, owner)] = "oauth-registration-changed"
                        self.catalog.invalidate_authorization_cache(row, GatewayError("auth_required", "OAuth 客户端注册已变更，请重新授权"),
                                                   owner if row.isolation == "user" else None)
                        invalidated.add(owner)
                    current_payload = await self.credential_store.load(row.id, config_owner, session=session)
                registration = {"config_fingerprint": fingerprint, "resource": resource, "mode": registration_mode, "auth": context}
                if current_payload.get("oauth_registration") != registration:
                    current_payload["oauth_registration"] = registration
                    await self.credential_store.write(session, current.id, config_owner, current_payload)
                await session.flush()
                token_payload = await self.credential_store.load(row.id, token_owner, session=session)
                epoch = token_payload.get("oauth_epoch", 0)
                failure_scopes = [await self.catalog._index_scope(session, current,
                    owner if row.isolation == "user" else "service") for owner in invalidated]
            for failure_scope in failure_scopes:
                await self.record_reauth_index(failure_scope)
            for owner in invalidated:
                await self.catalog.runtime.invalidate_credential(row.id, owner)
            await self.audit("oauth.client", user_id, row.id, registration_mode=registration_mode, result="ready")
            return effective, epoch

    async def exchange(self, auth, data):
        from .oauth_discovery import MAX_JSON_BYTES, SAFE_ERRORS

        endpoint = auth.get("token_url", "")
        check_url(endpoint)
        data = dict(data, client_id=auth.get("client_id", ""))
        if auth.get("resource"):
            data["resource"] = auth["resource"]
        from .oauth_discovery import auth_method

        try:
            method = auth_method(auth, auth)
        except GatewayError as exc:
            raise GatewayError("oauth_failed", str(exc)) from None
        basic = None
        if auth.get("client_secret"):
            if method == "client_secret_basic":
                basic = (quote(data.pop("client_id"), safe=""), quote(auth["client_secret"], safe=""))
            elif method == "client_secret_post":
                data["client_secret"] = auth["client_secret"]
            else:
                raise GatewayError("oauth_failed", "Public-client authentication cannot send a Client Secret")
        from urllib.parse import urlsplit

        from .oauth_config import local_host

        public_only = bool(auth.get("metadata_discovered") and not local_host(urlsplit(auth["resource"]).hostname))
        try:
            async with asyncio.timeout(TOKEN_REQUEST_TIMEOUT), token_http_client() as client:  # noqa: SIM117 - response uses client
                async with client.stream("POST", endpoint, data=data, auth=basic,
                                         extensions={"oauth_public_only": public_only}) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_JSON_BYTES:
                            raise GatewayError("oauth_failed", "OAuth token response exceeds 256 KiB")
                    status = response.status_code
        except (httpx.HTTPError, TimeoutError):
            raise GatewayError("oauth_failed", "OAuth token request failed or timed out; please reauthorize") from None
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise GatewayError("oauth_failed", "OAuth token endpoint returned invalid JSON") from None
        if not 200 <= status < 300:
            code = value.get("error") if isinstance(value, dict) else ""
            detail = " (" + code + ")" if isinstance(code, str) and code in SAFE_ERRORS else ""
            failure = GatewayError("oauth_failed", "OAuth token endpoint returned HTTP " + str(status) + detail +
                                   "；请检查 Client ID、Secret、回调地址或重新授权")
            # Only explicit client rejection can restart authorization; never replay an uncertain exchange.
            failure.oauth_error = code if status in {400, 401} and isinstance(code, str) and code in CIMD_REJECTIONS else None
            raise failure
        if not isinstance(value, dict) or not isinstance(value.get("access_token"), str) or not value["access_token"].strip():
            raise GatewayError("oauth_failed", "OAuth token response contains no valid access token")
        if str(value.get("token_type", "Bearer")).lower() != "bearer":
            raise GatewayError("oauth_failed", "OAuth token response uses an unsupported token type")
        if "refresh_token" in value and (not isinstance(value["refresh_token"], str) or not value["refresh_token"]):
            raise GatewayError("oauth_failed", "OAuth token response contains an invalid refresh token")
        try:
            if isinstance(value.get("expires_in"), bool):
                raise TypeError
            duration = float(value["expires_in"]) if value.get("expires_in") is not None else None
            if duration is not None and (not math.isfinite(duration) or duration < 0):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise GatewayError("oauth_failed", "OAuth token response has an invalid expiry") from None
        result = {key: value[key] for key in ("access_token", "refresh_token", "expires_in", "id_token", "token_type", "scope") if key in value}
        if "scope" in result and not isinstance(result["scope"], str):
            raise GatewayError("oauth_failed", "OAuth token scope must be a string")
        result["expires_at"] = time.time() + duration if duration is not None else None
        return result


USER = Depends(current_user)


async def oauth_actor(request, session, server_id, *, revision=None, read_only=False):
    user = await revalidate_user(request, session)
    row = await session.get(McpServer, server_id, populate_existing=True)
    if not row:
        raise HTTPException(404, "MCP not found")
    if revision is not None and row.revision != revision:
        raise HTTPException(409, "MCP configuration changed; restart authorization")
    if (
        user.role != "admin"
        and user.scope_mode != "all"
        and server_id not in user.mcp_ids
    ):
        raise HTTPException(403, "MCP access revoked")
    if row.isolation != "user" and user.role != "admin":
        raise HTTPException(403, "Only administrators may manage shared OAuth")
    config = await request.app.state.credentials.materialize(
        row, user.id, session=session, require=False
    )
    auth = config.get("auth", {})
    if auth.get("type") != "oauth":
        raise HTTPException(422, "MCP does not use OAuth")
    return user, row, auth


def oauth_authorizer(request, server_id, revision):
    async def authorize():
        async with request.app.state.db.locked() as session:
            user, _, _ = await oauth_actor(
                request, session, server_id, revision=revision
            )
            return user

    return authorize


@router.get("/mcps/{server_id}/oauth/status")
async def status(server_id: str, request: Request, user=USER):
    await request.app.state.oauth.sync_public_url(user.id)
    app = request.app.state
    async with app.db.locked() as session:
        user, row, _auth = await oauth_actor(request, session, server_id, read_only=True)
        payload = await app.credentials.load(
            row.id, credential_owner(row.isolation, user.id), session=session)
    if oauth_auth_status(payload) == "expired" and payload.get("oauth_token", {}).get("refresh_token"):
        try:
            await app.oauth.credentials(row, user.id)
        except GatewayError as exc:
            if exc.code != "auth_required":
                raise
            # Failed refresh persists needs_reauth; report that fresh state below.
    async with app.db.locked() as session:
        user, row, auth = await oauth_actor(request, session, server_id, read_only=True)
        owner = credential_owner(row.isolation, user.id)
        payload = await app.credentials.load(row.id, owner, session=session)
        auth_status = oauth_auth_status(payload)
        authorized = auth_status == "valid"
        cache = app.catalog.cached(row, user.id)
        state = ("needs_reauth" if payload.get("oauth_state") or auth_status == "expired" else
                 "pending_authorization" if not authorized else
                 "failed" if cache.get("cache_status") in {"error", "auth_required"} else "ready")
        configuration = await app.credentials.load(
            row.id, await app.oauth.registration_owner(row, user.id, session=session), session=session)
        registration = configuration.get("oauth_registration") or payload.get("oauth_token_client", {})
        return {"authorized": authorized, "state": state, "auth_status": auth_status,
                "refresh_available": bool(payload.get("oauth_token", {}).get("refresh_token")
                                          and auth_status != "needs_reauth"),
                "registration_mode": registration.get("mode", "manual"),
                "config_isolation": config_isolation({"auth": auth}), "isolation": row.isolation}


@router.post("/mcps/{server_id}/oauth/refresh")
async def refresh_authorization(server_id: str, request: Request, user=USER):
    app = request.app.state
    async with app.db.locked() as session:
        user, row, _auth = await oauth_actor(request, session, server_id)
    await app.oauth.credentials(row, user.id, force_refresh=True)
    return await status(server_id, request, user)


@router.post("/mcps/{server_id}/oauth/start")
async def start(server_id: str, request: Request, user=USER):
    app = request.app.state
    return_to = "mcps"
    if request.headers.get("content-length", "0") not in {"", "0"}:
        data = await request.json()
        if data.get("return_to") == "profile":
            return_to = "profile"
    async with app.db.locked() as session:
        user, row, auth = await oauth_actor(request, session, server_id)
        owner = credential_owner(row.isolation, user.id)
        payload = await app.credentials.load(row.id, owner, session=session)
        epoch = payload.get("oauth_epoch", 0)
    return await authorization_start(request, row, auth, user, epoch, return_to)


async def authorization_start(request, row, auth, user, epoch, return_to, *, fallback_reason=None, flow_generation=None):
    """Create a fresh state/PKCE flow, including a single policy-authorized DCR restart."""
    from .oauth_config import callback_path, callback_uri

    app = request.app.state
    await app.oauth.sync_public_url(user.id)
    owner = credential_owner(row.isolation, user.id)
    async with app.db.locked() as session:
        user, _, current_auth = await oauth_actor(request, session, row.id, revision=row.revision)
        if configuration_fingerprint(current_auth) != configuration_fingerprint(auth):
            raise state_error("state_mismatch", 409)
        if flow_generation is None:
            flow_generation = app.oauth.flows.begin(row.id, owner)
        else:
            app.oauth.flows.check(row.id, owner, flow_generation)
    callback_binding = callback_uri(app.config.public_url, auth)
    effective, epoch = await app.oauth.prepare(
        row, user.id, auth, request, epoch, fallback_reason=fallback_reason, flow_generation=flow_generation)
    async with app.db.locked() as session:
        user, row, current_auth = await oauth_actor(request, session, row.id, revision=row.revision)
        payload = await app.credentials.load(row.id, owner, session=session)
        if (configuration_fingerprint(current_auth) != configuration_fingerprint(auth)
                or payload.get("oauth_epoch", 0) != epoch
                or callback_uri(app.config.public_url, current_auth) != callback_binding):
            raise HTTPException(409, "OAuth configuration or authorization changed; restart authorization")
        app.oauth.flows.check(row.id, owner, flow_generation)
        check_url(effective.get("authorization_url", ""))
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        nonce = secrets.token_urlsafe(32)
        callback_url = callback_uri(app.config.public_url, auth)
        app.oauth.flows.prune()
        app.oauth.pending[nonce] = {
            "server_id": row.id, "user_id": user.id, "session_id": request.state.session_id,
            "verifier": verifier, "redirect_uri": callback_url, "expires": time.time() + STATE_TTL,
            "owner": owner, "flow_generation": flow_generation, "callback_path": callback_path(auth),
            "client_binding": app.oauth.client_binding(auth, effective, app.oauth.resource(row)),
            "revision": row.revision, "fingerprint": configuration_fingerprint(auth),
            "return_to": return_to, "epoch": epoch, "auth": effective,
            "registration_mode": effective.get("registration_mode", "manual"),
        }
        scopes = effective.get("scopes", [])
        scope = " ".join(scopes) if isinstance(scopes, list) else scopes
        supported = effective.get("scopes_supported", [])
        if isinstance(supported, list) and "offline_access" in supported and "offline_access" not in scope.split():
            scope = (scope + " offline_access").strip()
        params = {"response_type": "code", "client_id": effective.get("client_id", ""),
                  "redirect_uri": callback_url, "state": nonce, "code_challenge": challenge,
                  "code_challenge_method": "S256", "scope": scope}
        if effective.get("resource"):
            params["resource"] = effective["resource"]
        separator = "&" if "?" in effective["authorization_url"] else "?"
        result = {"authorization_url": effective["authorization_url"] + separator + urlencode(params)}
    await app.oauth.audit("oauth.start", user.id, row.id, result="pending")
    return result


async def callback_failure(request, state, reason, user_id=None, *, state_reason=None):
    context = request.app.state.oauth.flows.context(state)
    await request.app.state.oauth.audit(
        "oauth.callback_failed", user_id, context.get("server_id"),
        reason="state_mismatch" if reason == "state_used" else reason,
        state_reason=state_reason or reason, state_prefix=state[:8],
    )
    request.state.oauth_callback_failure_audited = True


@router.get("/oauth/callback")
@public_router.get("/callback", include_in_schema=False)
async def callback(request: Request, state: str = "", code: str = "", error: str = "", iss: str = "", user=USER):
    try:
        await request.app.state.oauth.sync_public_url(user.id)
        return await authorization_callback(request, state, code, error, iss, user)
    except (HTTPException, GatewayError, httpx.HTTPError) as exc:
        reason = getattr(exc, "oauth_reason", "state_mismatch")
        if not getattr(request.state, "oauth_callback_failure_audited", False):
            await callback_failure(request, state, reason, user.id if user else None,
                                   state_reason=getattr(exc, "oauth_state_reason", None))
        if isinstance(exc, HTTPException) and not getattr(exc, "oauth_reason", None):
            exc.detail = "OAuth state_mismatch: " + str(exc.detail)
        raise


async def authorization_callback(request, state, code, error, iss, user):
    from .oauth_config import callback_uri

    app = request.app.state
    async with app.db.locked() as session:
        user = await revalidate_user(request, session)
        pending = app.oauth.flows.consume(state, user.id, request.state.session_id,
                                         request.scope["route"].path)
        expected_issuer = pending["auth"].get("issuer_url", "")
        if (iss and iss != expected_issuer) or (pending["auth"].get("iss_required") and not iss):
            failure = state_error("state_mismatch")
            failure.oauth_state_reason = "issuer_mismatch"
            raise failure
        user, row, auth = await oauth_actor(
            request,
            session,
            pending["server_id"],
            revision=pending["revision"],
        )
        owner = credential_owner(row.isolation, user.id)
        payload = await app.credentials.load(row.id, owner, session=session)
        if (configuration_fingerprint(auth) != pending["fingerprint"] or payload.get("oauth_epoch", 0) != pending["epoch"]
                or (pending["auth"].get("resource") or "") != app.oauth.resource(row)
                or pending["redirect_uri"] != callback_uri(app.config.public_url, auth)):
            raise state_error("state_mismatch", 409)

    async def fallback(reason):
        from .oauth_config import registration_policy

        if (pending.get("registration_mode") != "cimd"
                or registration_policy(auth) != "prefer_cimd_fallback_dcr"):
            return None
        if pending["expires"] <= time.time():
            raise state_error("state_expired")
        app.oauth.flows.check(row.id, owner, pending["flow_generation"])
        result = await authorization_start(request, row, auth, user, pending["epoch"],
                                           pending["return_to"], fallback_reason=reason,
                                           flow_generation=pending["flow_generation"])
        return RedirectResponse(result["authorization_url"], status_code=303)

    if error:
        await callback_failure(request, state, "code_exchange_failed", user.id)
        if error in CIMD_REJECTIONS:
            redirected = await fallback(error)
            if redirected is not None:
                return redirected
        raise state_error("code_exchange_failed", 400)
    if not code:
        raise state_error("code_exchange_failed", 400)
    try:
        value = await app.oauth.exchange(
            pending["auth"],
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": pending["redirect_uri"],
                "code_verifier": pending["verifier"],
                **({"resource": pending["auth"]["resource"]} if pending["auth"].get("resource") else {}),
            },
        )
    except (GatewayError, httpx.HTTPError) as exc:
        await callback_failure(request, state, "code_exchange_failed", user.id)
        exc.oauth_reason = "code_exchange_failed"
        if getattr(exc, "oauth_error", None) in CIMD_REJECTIONS:
            redirected = await fallback(exc.oauth_error)
            if redirected is not None:
                return redirected
        raise
    async with app.db.locked() as session:
        user, row, auth = await oauth_actor(
            request,
            session,
            pending["server_id"],
            revision=pending["revision"],
        )
        app.oauth.flows.check(row.id, credential_owner(row.isolation, user.id), pending["flow_generation"])
        payload = await app.credentials.load(row.id, credential_owner(row.isolation, user.id), session=session)
        if (
            payload.get("oauth_epoch", 0) != pending["epoch"]
            or pending["expires"] < time.time()
            or pending["session_id"] != request.state.session_id
            or configuration_fingerprint(auth) != pending["fingerprint"]
            or (pending["auth"].get("resource") or "") != app.oauth.resource(row)
            or pending["redirect_uri"] != callback_uri(app.config.public_url, auth)
        ):
            raise state_error("state_expired" if pending["expires"] <= time.time() else "state_mismatch")
        owner = await app.credentials.save_oauth_token(
            session, row, user.id, value, client=pending["client_binding"]
        )
    await app.runtime.invalidate_credential(row.id, owner)
    await app.oauth.audit("oauth.authorized", user.id, row.id, result="success")
    if row.mode != "disabled":
        try:
            await app.catalog.refresh_index(
                row.id,
                user.id,
                authorize=oauth_authorizer(
                    request, row.id, pending["revision"]
                ),
            )
        except GatewayError:
            pass
    destination = "profile" if pending["return_to"] == "profile" else "mcps"
    return RedirectResponse(
        app.config.public_url.rstrip("/") + "/#/" + destination,
        status_code=303,
    )


@router.post("/mcps/{server_id}/oauth/reset")
async def reset(server_id: str, request: Request, user=USER):
    from .oauth_config import allows_dcr, callback_uri, registration_matches

    app = request.app.state
    await app.oauth.sync_public_url(user.id)
    async with app.db.locked() as session:
        user, row, auth = await oauth_actor(request, session, server_id)
        owner = credential_owner(row.isolation, user.id)
        registration_owner = await app.oauth.registration_owner(row, user.id, session=session)
        configuration = await app.credentials.load(row.id, registration_owner, session=session)
        registration = configuration.get("oauth_registration", {})
        payload = (configuration if registration_owner == owner
                   else await app.credentials.load(row.id, owner, session=session))
        if (token_from_auth(auth).get("access_token") and not payload.get("oauth_token_client")
                and registration_matches(registration, auth, app.oauth.resource(row),
                                         callback_uri(app.config.public_url, auth))):
            payload["oauth_token_client"] = copy.deepcopy(registration)
        cleared_client = registration.get("mode") == "dcr"
        if row.isolation == "user" and app.oauth.config_owner(row, user.id) != owner and allows_dcr(auth):
            # Reset only this user's future registration, never other users' shared client.
            payload["oauth_registration_private"] = True
        if cleared_client and registration_owner == owner:
            payload.pop("oauth_registration", None)
        cleared_states = app.oauth.flows.invalidate(row.id, owner)
        await app.credentials.write(session, row.id, owner, payload)
    await app.oauth.audit("oauth.reset", user.id, row.id,
                          cleared_states=cleared_states, cleared_client=cleared_client)
    return {
        "cleared_states": cleared_states, "cleared_client": cleared_client,
        "message": "已清理未完成的授权流程，请关闭所有旧授权标签页，再重新发起授权。",
    }


@router.post("/mcps/{server_id}/oauth/disconnect")
async def disconnect(server_id: str, request: Request, user=USER):
    app = request.app.state
    async with app.db.locked() as session:
        user, row, _ = await oauth_actor(request, session, server_id)
        owner = credential_owner(row.isolation, user.id)
        payload = await app.credentials.load(row.id, owner, session=session)
        payload.pop("oauth_token", None)
        payload.pop("oauth_token_client", None)
        app.oauth.flows.invalidate(row.id, owner)
        payload["oauth_epoch"] = payload.get("oauth_epoch", 0) + 1
        payload["oauth_state"] = {"status": "needs_reauth", "reason": "disconnected", "at": time.time()}
        await app.credentials.write(session, row.id, owner, payload)
        legacy = await session.get(
            SystemSetting, app.oauth.key(row, user.id)
        )
        if legacy:
            await session.delete(legacy)
    await app.runtime.invalidate_credential(server_id, owner)
    app.catalog.invalidate_authorization_cache(
        row,
        GatewayError(
            "auth_required",
            "请在 MCP 服务更多或个人资料内点击 OAuth 授权完成 MCP 认证",
        ),
        user.id if row.isolation == "user" else None,
    )
    await app.oauth.record_reauth_index(await app.catalog.index_scope(row, user.id))
    return {"ok": True}


@public_router.get("/oauth/client-metadata.json")
async def public_metadata(request: Request):
    from fastapi.responses import JSONResponse

    from .oauth_config import client_metadata

    return JSONResponse(client_metadata(request.app.state.config.public_url),
                        headers={"Cache-Control": "public, max-age=3600"})
