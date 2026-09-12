"""OAuth authorization code with PKCE, isolated sealed credentials and refresh."""
import asyncio
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .database import McpServer, SystemSetting, get_setting
from .identity import current_user, revalidate_user
from .runtime import GatewayError
from .transports import check_url

router = APIRouter(prefix="/api/v1")


class OAuth:
    def __init__(self, catalog):
        self.catalog = catalog
        self.pending = {}
        self.locks = {}

    def key(self, row, user_id):
        config = self.catalog.unseal(row.config).get("auth", {})
        scope = user_id if config.get("scope") == "user" else "service"
        return "oauth:" + row.id + ":" + str(scope)

    async def credentials(self, row, user_id):
        key = self.key(row, user_id)
        async with self.locks.setdefault(key, asyncio.Lock()):
            stored = await get_setting(self.catalog.db, key)
            if not stored:
                raise GatewayError("auth_required", "Complete OAuth authorization in the Web console")
            value = self.catalog.unseal(stored)
            if value.get("expires_at", float("inf")) <= time.time() + 60:
                if not value.get("refresh_token"):
                    raise GatewayError("auth_required", "OAuth authorization expired")
                auth = self.catalog.unseal(row.config)["auth"]
                value = value | await self.exchange(auth, {"grant_type": "refresh_token",
                                                          "refresh_token": value["refresh_token"]})
                async with self.catalog.db.locked() as session:
                    current = await session.get(SystemSetting, key)
                    server = await session.get(McpServer, row.id)
                    if not current or current.value != stored or not server or server.revision != row.revision:
                        raise GatewayError("auth_required", "OAuth authorization changed during refresh")
                    current.value = self.catalog.seal(value)
                    owner = user_id if auth.get("scope") == "user" else "service"
                    self.catalog.runtime.credential_versions[(row.id, owner)] = hashlib.sha256(
                        value["access_token"].encode()).hexdigest()[:16]
            return value

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
        async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
            response = await client.post(endpoint, data=data, auth=basic)
        if response.status_code != 200:
            raise GatewayError("oauth_failed", "OAuth token endpoint returned HTTP " + str(response.status_code))
        value = response.json()
        if not value.get("access_token"):
            raise GatewayError("oauth_failed", "OAuth token response contains no access token")
        value["expires_at"] = time.time() + float(value["expires_in"]) if value.get("expires_in") else float("inf")
        return value


USER = Depends(current_user)


async def oauth_actor(request, session, server_id, *, revision=None, read_only=False):
    user = await revalidate_user(request, session)
    row = await session.get(McpServer, server_id, populate_existing=True)
    if not row:
        raise HTTPException(404, "MCP not found")
    if revision is not None and row.revision != revision:
        raise HTTPException(409, "MCP configuration changed; restart authorization")
    # OAuth repair does not start an MCP or change its disabled policy.
    if user.role != "admin" and user.scope_mode != "all" and server_id not in user.mcp_ids:
        raise HTTPException(403, "MCP access revoked")
    auth = request.app.state.catalog.unseal(row.config).get("auth", {})
    if auth.get("type") != "oauth":
        raise HTTPException(422, "MCP does not use OAuth")
    if not read_only and auth.get("scope", "service") != "user" and user.role != "admin":
        raise HTTPException(403, "Service OAuth requires administrator")
    return user, row, auth


def oauth_authorizer(request, server_id, revision):
    async def authorize():
        async with request.app.state.db.locked() as s:
            user, _, _ = await oauth_actor(request, s, server_id, revision=revision)
            return user
    return authorize


async def store_credentials(session, key, value):
    item = await session.get(SystemSetting, key)
    if item:
        item.value = value
    else:
        session.add(SystemSetting(key=key, value=value))


@router.get("/mcps/{server_id}/oauth/status")
async def status(server_id: str, request: Request, user=USER):
    app = request.app.state
    async with app.db.locked() as s:
        user, row, auth = await oauth_actor(request, s, server_id, read_only=True)
        item = await s.get(SystemSetting, app.oauth.key(row, user.id))
        value = app.catalog.unseal(item.value) if item and item.value else {}
        authorized = bool(value.get("access_token") and (
            value.get("refresh_token") or value.get("expires_at", float("inf")) > time.time()))
        return {"authorized": authorized, "scope": auth.get("scope", "service")}


@router.post("/mcps/{server_id}/oauth/start")
async def start(server_id: str, request: Request, user=USER):
    state = request.app.state
    async with state.db.locked() as s:
        user, row, auth = await oauth_actor(request, s, server_id)
        check_url(auth.get("authorization_url", ""))
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        nonce = secrets.token_urlsafe(32)
        callback = state.config.public_url.rstrip("/") + "/api/v1/oauth/callback"
        state.oauth.pending = {k: v for k, v in state.oauth.pending.items() if v["expires"] > time.time()}
        state.oauth.pending[nonce] = {"server_id": server_id, "user_id": user.id,
                                     "session_id": request.state.session_id, "verifier": verifier,
                                     "redirect_uri": callback, "expires": time.time() + 600,
                                     "revision": row.revision}
        scopes = auth.get("scopes", [])
        params = {"response_type": "code", "client_id": auth.get("client_id", ""), "redirect_uri": callback,
                  "state": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
                  "scope": " ".join(scopes) if isinstance(scopes, list) else scopes}
        return {"authorization_url": auth["authorization_url"] + ("&" if "?" in auth["authorization_url"] else "?")
                + urlencode(params)}


@router.get("/oauth/callback")
async def callback(request: Request, state: str, code: str = "", error: str = "", user=USER):
    app = request.app.state
    async with app.db.locked() as s:
        user = await revalidate_user(request, s)
        pending = app.oauth.pending.get(state)
        if (not pending or pending["expires"] < time.time() or pending["user_id"] != user.id
                or pending["session_id"] != request.state.session_id):
            raise HTTPException(403, "OAuth state is invalid or expired")
        app.oauth.pending.pop(state)
        if error or not code:
            raise HTTPException(400, "OAuth authorization was declined")
        user, row, auth = await oauth_actor(request, s, pending["server_id"], revision=pending["revision"])
    # No database mutex is held while the remote token endpoint is contacted.
    value = await app.oauth.exchange(auth, {"grant_type": "authorization_code", "code": code,
                                          "redirect_uri": pending["redirect_uri"],
                                          "code_verifier": pending["verifier"]})
    async with app.db.locked() as s:
        user, row, auth = await oauth_actor(request, s, pending["server_id"], revision=pending["revision"])
        if pending["expires"] < time.time() or pending["session_id"] != request.state.session_id:
            raise HTTPException(403, "OAuth state is invalid or expired")
        await store_credentials(s, app.oauth.key(row, user.id), app.catalog.seal(value))
        owner = user.id if auth.get("scope") == "user" else "service"
        app.runtime.credential_versions[(row.id, owner)] = hashlib.sha256(
            value["access_token"].encode()).hexdigest()[:16]
    if row.mode == "disabled":
        return RedirectResponse(app.config.public_url.rstrip("/") + "/#/mcps", status_code=303)
    try:
        await app.catalog.refresh(row.id, user.id,
                                  authorize=oauth_authorizer(request, row.id, pending["revision"]))
    except GatewayError:
        # Authorization succeeded; the service list shows the discovery failure.
        pass
    return RedirectResponse(app.config.public_url.rstrip("/") + "/#/mcps", status_code=303)


@router.post("/mcps/{server_id}/oauth/disconnect")
async def disconnect(server_id: str, request: Request, user=USER):
    app = request.app.state
    async with app.db.locked() as s:
        user, row, auth = await oauth_actor(request, s, server_id)
        await store_credentials(s, app.oauth.key(row, user.id), None)
        owner = user.id if auth.get("scope") == "user" else "service"
        app.runtime.credential_versions[(row.id, owner)] = None
    await app.runtime.invalidate_credential(server_id, owner)
    app.catalog.cache_failure(row, GatewayError("auth_required",
        "请在 MCP 服务更多或个人资料内点击 OAuth 授权完成 MCP 认证"), user.id)
    return {"ok": True}
