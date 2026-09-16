"""OAuth authorization code with PKCE and isolation-aware sealed credentials."""
import asyncio
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .credentials import config_isolation, credential_owner
from .database import McpServer, SystemSetting
from .identity import current_user, revalidate_user
from .runtime import GatewayError
from .transports import check_url

router = APIRouter(prefix="/api/v1")


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

    async def credentials(self, row, user_id):
        owner = credential_owner(row.isolation, user_id)
        key = self.credential_store.key(row.id, owner)
        async with self.locks.setdefault(key, asyncio.Lock()):
            config = await self.credential_store.materialize(row, user_id)
            auth = config.get("auth", {})
            value = token_from_auth(auth)
            if not value.get("access_token"):
                raise GatewayError(
                    "auth_required",
                    "Complete OAuth authorization in the Web console",
                )
            if value.get("expires_at", float("inf")) > time.time() + 60:
                return value
            if not value.get("refresh_token"):
                raise GatewayError("auth_required", "OAuth authorization expired")

            refreshed = value | await self.exchange(
                auth,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": value["refresh_token"],
                },
            )
            async with self.catalog.db.locked() as session:
                current = await self.credential_store.load(
                    row.id, owner, session=session
                )
                server = await session.get(McpServer, row.id)
                if (
                    current.get("oauth_token") != value
                    or not server
                    or server.revision != row.revision
                ):
                    raise GatewayError(
                        "auth_required",
                        "OAuth authorization changed during refresh",
                    )
                await self.credential_store.save_oauth_token(
                    session, server, user_id, refreshed
                )
            await self.catalog.runtime.invalidate_credential(row.id, owner)
            return refreshed

    async def exchange(self, auth, data):
        endpoint = auth.get("token_url", "")
        check_url(endpoint)
        data["client_id"] = auth.get("client_id", "")
        method = auth.get("token_endpoint_auth_method", "client_secret_post")
        basic = None
        if auth.get("client_secret"):
            if method == "client_secret_basic":
                basic = (data.pop("client_id"), auth["client_secret"])
            else:
                data["client_secret"] = auth["client_secret"]
        async with httpx.AsyncClient(
            timeout=30, trust_env=False, follow_redirects=False
        ) as client:
            response = await client.post(endpoint, data=data, auth=basic)
        if response.status_code != 200:
            raise GatewayError(
                "oauth_failed",
                "OAuth token endpoint returned HTTP " + str(response.status_code),
            )
        value = response.json()
        if not value.get("access_token"):
            raise GatewayError(
                "oauth_failed", "OAuth token response contains no access token"
            )
        value["expires_at"] = (
            time.time() + float(value["expires_in"])
            if value.get("expires_in")
            else float("inf")
        )
        return value


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
    if not auth.get("authorization_url") or not auth.get("token_url"):
        raise GatewayError(
            "auth_required", "OAuth configuration is not available"
        )
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
        user, row, auth = await oauth_actor(
            request, session, server_id, read_only=True
        )
        owner = credential_owner(row.isolation, user.id)
        payload = await app.credentials.load(row.id, owner, session=session)
        value = payload.get("oauth_token", {})
        authorized = bool(
            value.get("access_token")
            and (
                value.get("refresh_token")
                or value.get("expires_at", float("inf")) > time.time()
            )
        )
        cache = app.catalog.cached(row, user.id)
        oauth_state = (
            "pending_authorization"
            if not authorized
            else "failed"
            if cache.get("cache_status") in {"error", "auth_required"}
            else "ready"
        )
        return {
            "authorized": authorized,
            "state": oauth_state,
            "config_isolation": config_isolation({"auth": auth}),
            "isolation": row.isolation,
        }


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
        check_url(auth.get("authorization_url", ""))
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        nonce = secrets.token_urlsafe(32)
        callback_url = (
            app.config.public_url.rstrip("/") + "/api/v1/oauth/callback"
        )
        app.oauth.pending = {
            key: value
            for key, value in app.oauth.pending.items()
            if value["expires"] > time.time()
        }
        app.oauth.pending[nonce] = {
            "server_id": server_id,
            "user_id": user.id,
            "session_id": request.state.session_id,
            "verifier": verifier,
            "redirect_uri": callback_url,
            "expires": time.time() + 600,
            "revision": row.revision,
            "fingerprint": configuration_fingerprint(auth),
            "return_to": return_to,
        }
        scopes = auth.get("scopes", [])
        params = {
            "response_type": "code",
            "client_id": auth.get("client_id", ""),
            "redirect_uri": callback_url,
            "state": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": " ".join(scopes) if isinstance(scopes, list) else scopes,
        }
        separator = "&" if "?" in auth["authorization_url"] else "?"
        return {
            "authorization_url": (
                auth["authorization_url"] + separator + urlencode(params)
            )
        }


@router.get("/oauth/callback")
async def callback(
    request: Request,
    state: str,
    code: str = "",
    error: str = "",
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
        if error or not code:
            raise HTTPException(400, "OAuth authorization was declined")
        user, row, auth = await oauth_actor(
            request,
            session,
            pending["server_id"],
            revision=pending["revision"],
        )
        if configuration_fingerprint(auth) != pending["fingerprint"]:
            raise HTTPException(
                409, "OAuth configuration changed; restart authorization"
            )

    value = await app.oauth.exchange(
        auth,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["verifier"],
        },
    )
    async with app.db.locked() as session:
        user, row, auth = await oauth_actor(
            request,
            session,
            pending["server_id"],
            revision=pending["revision"],
        )
        if (
            pending["expires"] < time.time()
            or pending["session_id"] != request.state.session_id
            or configuration_fingerprint(auth) != pending["fingerprint"]
        ):
            raise HTTPException(403, "OAuth state is invalid or expired")
        owner = await app.credentials.save_oauth_token(
            session, row, user.id, value
        )
    await app.runtime.invalidate_credential(row.id, owner)
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
