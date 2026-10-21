"""OAuth authorization code with PKCE and isolation-aware sealed credentials."""
import asyncio
import base64
import hashlib
import json
import math
import secrets
import time
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .credentials import config_isolation, credential_owner
from .database import McpServer, SystemSetting
from .identity import current_user, revalidate_user
from .runtime import GatewayError
from .transports import check_url

router = APIRouter(prefix="/api/v1")
TOKEN_REQUEST_TIMEOUT = 30


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
        self.pending = {}
        self.locks = {}

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
            payload["oauth_epoch"] = payload.get("oauth_epoch", 0) + 1
            payload["oauth_state"] = {"status": "needs_reauth", "reason": reason, "at": time.time()}
            await self.credential_store.write(session, row.id, owner, payload)
            self.catalog.runtime.credential_versions[(row.id, owner)] = "oauth-needs-reauth"
            scope = owner if row.isolation == "user" else None
            self.catalog.cache_failure(row, GatewayError("auth_required", "授权已失效，请点击重新授权以继续使用"), scope)
            if reason in {"token_expired", "refresh_failed"}:
                from .database import now

                cached = self.catalog.cached(row, scope)
                cached.update(last_refresh_error="授权已失效，请重新授权", last_refresh_error_code="auth_required",
                              last_refresh_failure_at=now().isoformat())
                self.catalog.save_cache(row, cached, scope)
            await session.flush()
            failure_scope = await self.catalog._index_scope(session, server, owner if row.isolation == "user" else "service")
        await self.record_reauth_index(failure_scope)
        return True

    async def record_reauth_index(self, scope):
        try:
            expected = await self.catalog.index.refresh_version(scope)
            await self.catalog.index.record_failure(scope, {
                "cache_error_code": "auth_required", "cache_error": "授权已失效，请重新授权",
            }, expected_refresh=expected)
        except GatewayError as exc:
            if exc.code not in {"credential_changed", "revision_changed", "disabled", "generation_superseded"}:
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

    async def credentials(self, row, user_id, *, with_epoch=False):
        owner = credential_owner(row.isolation, user_id)
        key = self.credential_store.key(row.id, owner)
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
            registration = (await self.credential_store.load(row.id, self.config_owner(row, user_id))).get("oauth_registration")
            if registration:
                try:
                    await self.effective_auth(row, user_id, auth)
                except GatewayError as exc:
                    reason = "binding_changed" if exc.code == "auth_required" else "client_registration_invalid"
                    await self.mark_reauth(row, owner, value, reason, epoch=epoch)
                    raise GatewayError("auth_required", str(exc)) from exc
            if token_expiry(value) > time.time() + 60:
                return (value, epoch) if with_epoch else value
            if not value.get("refresh_token"):
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
                await self.credential_store.save_oauth_token(session, server, user_id, refreshed)
                epoch += 1
                self.catalog.runtime.credential_versions[(row.id, owner)] = None
            await self.catalog.runtime.invalidate_credential(row.id, owner)
            await self.audit("oauth.refresh", user_id, row.id, result="success")
            return (refreshed, epoch) if with_epoch else refreshed

    def config_owner(self, row, user_id):
        return credential_owner(row.isolation, user_id) if row.isolation == "user" and row.config_isolation == "user" else "service"

    async def effective_auth(self, row, user_id, auth):
        from .oauth_config import callback_uri, registration_fingerprint

        payload = await self.credential_store.load(row.id, self.config_owner(row, user_id))
        registration = payload.get("oauth_registration", {})
        resource = self.resource(row)
        fingerprint = registration_fingerprint(auth, resource, callback_uri(self.catalog.config.public_url))
        if registration.get("config_fingerprint") == fingerprint:
            from .oauth_discovery import auth_method

            effective = auth | registration["auth"]
            auth_method(effective, effective)
            return effective
        if registration or auth.get("auth_mode") in {"auto", "dcr"} or not all(auth.get(key) for key in ("authorization_url", "token_url")):
            raise GatewayError("auth_required", "OAuth 客户端信息需要重新授权，请在控制台点击重新授权")
        return auth

    async def prepare(self, row, user_id, auth, request, epoch):
        from .oauth_config import allows_dcr, callback_uri, client_metadata, registration_fingerprint
        from .oauth_discovery import auth_method, canonical_resource, discover, register, registration_method

        config_owner = self.config_owner(row, user_id)
        token_owner = credential_owner(row.isolation, user_id)
        lock = "registration:" + self.credential_store.key(row.id, config_owner)
        redirect_uri = callback_uri(self.catalog.config.public_url)
        resource = self.resource(row)
        fingerprint = registration_fingerprint(auth, resource, redirect_uri)
        async with self.locks.setdefault(lock, asyncio.Lock()):
            payload = await self.credential_store.load(row.id, config_owner)
            cached = payload.get("oauth_registration", {})
            mode = auth.get("auth_mode", "manual")
            if mode == "manual" and all(auth.get(key) for key in ("authorization_url", "token_url")):
                context = {key: auth[key] for key in ("authorization_url", "token_url", "issuer_url", "scopes") if key in auth}
                if resource and row.transport in {"streamable-http", "sse"}:
                    context["resource"] = canonical_resource(resource)
            else:
                context = await discover(resource, auth)
            async with self.catalog.db.locked() as session:
                _, _, current_auth = await oauth_actor(request, session, row.id, revision=row.revision)
                current_token = await self.credential_store.load(row.id, token_owner, session=session)
                if (configuration_fingerprint(current_auth) != configuration_fingerprint(auth)
                        or current_token.get("oauth_epoch", 0) != epoch or self.resource(row) != resource):
                    raise HTTPException(409, "OAuth authorization changed during discovery")
            registration_mode = "manual"
            identity = auth.get("client_id", "")
            if mode in {"auto", "dcr"}:
                if context.get("cimd_supported"):
                    if auth.get("token_endpoint_auth_method") not in {None, "none"}:
                        raise GatewayError("oauth_configuration_required",
                            "CIMD 当前使用公开客户端 none，不能使用所选 Secret 认证方式；请选择自动/none 或使用预注册")
                    context["token_endpoint_auth_method"] = auth_method({"token_endpoint_auth_method": "none"}, context)
                    identity = client_metadata(self.catalog.config.public_url)["client_id"]
                    registration_mode = "cimd"
                elif allows_dcr(auth) and context.get("registration_url"):
                    registration_mode = "dcr"
                    requested = registration_method(auth, context)
                    registered = None
                    if (cached.get("config_fingerprint") == fingerprint and cached.get("mode") == "dcr"
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
                        }, auth["client_name"], redirect_uri)
                    context.update(registered)
                    identity = registered["client_id"]
                else:
                    raise GatewayError("oauth_configuration_required",
                        "服务器未提供可用的 CIMD。请填写预注册 Client ID；仅在服务器提供注册端点时可启用 DCR 回退")
            if mode != "manual" and not identity:
                raise GatewayError("oauth_configuration_required", "OAuth Client ID is unavailable")
            context["client_id"] = identity
            context["token_endpoint_auth_method"] = auth_method(auth if mode == "manual" else context, context)
            effective = auth | context
            invalidated = set()
            async with self.catalog.db.locked() as session:
                _, current, current_auth = await oauth_actor(request, session, row.id, revision=row.revision)
                token_payload = await self.credential_store.load(row.id, token_owner, session=session)
                if (configuration_fingerprint(current_auth) != configuration_fingerprint(auth)
                        or token_payload.get("oauth_epoch", 0) != epoch or self.resource(current) != resource):
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
                        item.pop("oauth_token", None)
                        item["oauth_epoch"] = item.get("oauth_epoch", 0) + 1
                        item["oauth_state"] = {"status": "needs_reauth", "reason": reason, "at": time.time()}
                        await self.credential_store.write(session, row.id, owner, item)
                        self.catalog.runtime.credential_versions[(row.id, owner)] = "oauth-registration-changed"
                        self.catalog.cache_failure(row, GatewayError("auth_required", "OAuth 客户端注册已变更，请重新授权"),
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
            raise GatewayError("oauth_failed", "OAuth token endpoint returned HTTP " + str(status) + detail +
                               "；请检查 Client ID、Secret、回调地址或重新授权")
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
    app = request.app.state
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
        configuration = await app.credentials.load(row.id, app.oauth.config_owner(row, user.id), session=session)
        return {"authorized": authorized, "state": state, "auth_status": auth_status,
                "registration_mode": configuration.get("oauth_registration", {}).get("mode", "manual"),
                "config_isolation": config_isolation({"auth": auth}), "isolation": row.isolation}


@router.post("/mcps/{server_id}/oauth/start")
async def start(server_id: str, request: Request, user=USER):
    from .oauth_config import callback_uri

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
    effective, epoch = await app.oauth.prepare(row, user.id, auth, request, epoch)
    async with app.db.locked() as session:
        user, row, current_auth = await oauth_actor(request, session, server_id, revision=row.revision)
        payload = await app.credentials.load(row.id, owner, session=session)
        if configuration_fingerprint(current_auth) != configuration_fingerprint(auth) or payload.get("oauth_epoch", 0) != epoch:
            raise HTTPException(409, "OAuth configuration or authorization changed; restart authorization")
        check_url(effective.get("authorization_url", ""))
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        nonce = secrets.token_urlsafe(32)
        callback_url = callback_uri(app.config.public_url)
        app.oauth.pending = {key: value for key, value in app.oauth.pending.items() if value["expires"] > time.time()}
        app.oauth.pending[nonce] = {
            "server_id": server_id, "user_id": user.id, "session_id": request.state.session_id,
            "verifier": verifier, "redirect_uri": callback_url, "expires": time.time() + 600,
            "revision": row.revision, "fingerprint": configuration_fingerprint(auth),
            "return_to": return_to, "epoch": epoch, "auth": effective,
        }
        scopes = effective.get("scopes", [])
        params = {"response_type": "code", "client_id": effective.get("client_id", ""),
                  "redirect_uri": callback_url, "state": nonce, "code_challenge": challenge,
                  "code_challenge_method": "S256", "scope": " ".join(scopes) if isinstance(scopes, list) else scopes}
        if effective.get("resource"):
            params["resource"] = effective["resource"]
        separator = "&" if "?" in effective["authorization_url"] else "?"
        result = {"authorization_url": effective["authorization_url"] + separator + urlencode(params)}
    await app.oauth.audit("oauth.start", user.id, row.id, result="pending")
    return result


@router.get("/oauth/callback")
async def callback(
    request: Request,
    state: str,
    code: str = "",
    error: str = "",
    iss: str = "",
    user=USER,
):
    app = request.app.state
    async with app.db.locked() as session:
        user = await revalidate_user(request, session)
        pending = app.oauth.pending.get(state)
        if (
            not pending
            or pending["expires"] < time.time()
            or pending["user_id"] != user.id
            or pending["session_id"] != request.state.session_id
        ):
            raise HTTPException(403, "OAuth state is invalid or expired")
        app.oauth.pending.pop(state)
        expected_issuer = pending["auth"].get("issuer_url", "")
        if (iss and iss != expected_issuer) or (pending["auth"].get("iss_required") and not iss):
            raise HTTPException(403, "OAuth authorization issuer does not match")
        if error or not code:
            raise HTTPException(400, "OAuth authorization was declined")
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
                or pending["redirect_uri"] != app.config.public_url.rstrip("/") + "/api/v1/oauth/callback"):
            raise HTTPException(
                409, "OAuth configuration changed; restart authorization"
            )

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
    async with app.db.locked() as session:
        user, row, auth = await oauth_actor(
            request,
            session,
            pending["server_id"],
            revision=pending["revision"],
        )
        payload = await app.credentials.load(row.id, credential_owner(row.isolation, user.id), session=session)
        if (
            payload.get("oauth_epoch", 0) != pending["epoch"]
            or pending["expires"] < time.time()
            or pending["session_id"] != request.state.session_id
            or configuration_fingerprint(auth) != pending["fingerprint"]
            or (pending["auth"].get("resource") or "") != app.oauth.resource(row)
            or pending["redirect_uri"] != app.config.public_url.rstrip("/") + "/api/v1/oauth/callback"
        ):
            raise HTTPException(403, "OAuth state is invalid or expired")
        owner = await app.credentials.save_oauth_token(
            session, row, user.id, value
        )
    await app.runtime.invalidate_credential(row.id, owner)
    await app.oauth.audit("oauth.authorized", user.id, row.id, result="success")
    if row.mode != "disabled":
        try:
            await app.catalog.refresh(
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


@router.post("/mcps/{server_id}/oauth/disconnect")
async def disconnect(server_id: str, request: Request, user=USER):
    app = request.app.state
    async with app.db.locked() as session:
        user, row, _ = await oauth_actor(request, session, server_id)
        owner = credential_owner(row.isolation, user.id)
        payload = await app.credentials.load(row.id, owner, session=session)
        payload.pop("oauth_token", None)
        payload["oauth_epoch"] = payload.get("oauth_epoch", 0) + 1
        payload["oauth_state"] = {"status": "needs_reauth", "reason": "disconnected", "at": time.time()}
        await app.credentials.write(session, row.id, owner, payload)
        legacy = await session.get(
            SystemSetting, app.oauth.key(row, user.id)
        )
        if legacy:
            await session.delete(legacy)
    await app.runtime.invalidate_credential(server_id, owner)
    app.catalog.cache_failure(
        row,
        GatewayError(
            "auth_required",
            "请在 MCP 服务更多或个人资料内点击 OAuth 授权完成 MCP 认证",
        ),
        user.id if row.isolation == "user" else None,
    )
    return {"ok": True}


public_router = APIRouter()


@public_router.get("/oauth/client-metadata.json")
async def public_metadata(request: Request):
    from fastapi.responses import JSONResponse

    from .oauth_config import client_metadata

    return JSONResponse(client_metadata(request.app.state.config.public_url),
                        headers={"Cache-Control": "public, max-age=3600"})
