import time

import httpx
import pytest

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import set_setting
from test_identity import signup, login


@pytest.fixture
async def auth_app(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="auth-bug1"))
    del app.state.protocol  # API-only fixture avoids crossing AnyIO scopes between pytest tasks.
    async with app.router.lifespan_context(app):
        from rest_fixture import rest_connect
        app.state.runtime.connector = rest_connect
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as web:
            user = await signup(web, "admin")
            await login(web, "admin")
            yield app, web, user


@pytest.mark.parametrize("action", ["delete", "rotate", "disable"])
async def test_token_mutation_releases_leases_and_sessions(auth_app, action):
    app, web, user = auth_app
    token = (await web.post("/api/v1/tokens", json={"name": "agent", "scope_mode": "all"})).json()
    headers = {"Authorization": "Bearer " + token["token"]}
    lease_id = (await web.post("/gateway/v1/leases", headers=headers)).json()["id"]
    app.state.gateway.sessions["session"] = {"owner": (user["id"], token["id"]), "lease": lease_id}
    if action == "delete":
        response = await web.delete("/api/v1/tokens/" + token["id"])
    elif action == "rotate":
        response = await web.post("/api/v1/tokens/" + token["id"] + "/rotate")
    else:
        response = await web.patch("/api/v1/tokens/" + token["id"], json={"disabled": True})
    assert response.status_code == 200
    assert (await web.post("/gateway/v1/leases/" + lease_id + "/heartbeat", headers=headers)).status_code == 401
    assert lease_id not in app.state.runtime.leases
    assert "session" not in app.state.gateway.sessions


async def test_anonymous_lease_requires_independent_client_secret(auth_app):
    app, web, _ = auth_app
    await set_setting(app.state.db, "token_auth_enabled", False)
    lease = (await web.post("/gateway/v1/leases")).json()
    endpoint = "/gateway/v1/leases/" + lease["id"] + "/heartbeat"
    assert (await web.post(endpoint)).status_code >= 400
    assert (await web.post(endpoint, headers={"X-MCP-Manager-Client": lease["client_secret"]})).status_code == 200


async def test_oauth_status_and_personal_refresh(auth_app, monkeypatch):
    app, web, user = auth_app
    row = await app.state.catalog.create({"name": "OAuth", "transport": "rest", "isolation": "user", "config": {
        "auth": {"type": "oauth", "scope": "user", "authorization_url": "https://auth.test/authorize",
                 "token_url": "https://auth.test/token"}, "tools": [{"name": "read", "request": {"url": "https://api.test/read"}}]}}, user_id=user["id"])
    endpoint = "/api/v1/mcps/" + row.id + "/oauth/status"
    assert (await web.get(endpoint)).json()["authorized"] is False
    await set_setting(app.state.db, app.state.oauth.key(row, user["id"]), app.state.catalog.seal(
        {"access_token": "old", "refresh_token": "refresh", "expires_at": 0}))
    async def exchange(auth, data):
        return {"access_token": "new", "expires_at": time.time() + 3600}
    monkeypatch.setattr(app.state.oauth, "exchange", exchange)
    credentials = await app.state.oauth.credentials(row, user["id"])
    assert credentials["access_token"] == "new"
    assert app.state.runtime.credential_versions[(row.id, user["id"])] is None
    row = await app.state.catalog.update(row.id, {"mode": "lazy"}, user_id=user["id"])
    spec = await app.state.catalog.spec(row, user["id"])
    assert spec.credential_owner == user["id"]
    assert app.state.runtime.credential_versions[(row.id, user["id"])]
    await app.state.catalog.warm(row.id, user["id"])
    target = next(target for target in await app.state.catalog.refresh_targets() if target["server_id"] == row.id)
    assert target["user_id"] == user["id"]
    await app.state.catalog.refresh_target(target)
    assert (await web.get(endpoint)).json()["authorized"] is True
    await web.post("/api/v1/mcps/" + row.id + "/oauth/disconnect")
    assert (await web.get(endpoint)).json()["authorized"] is False


async def test_oauth_callback_redirects_after_authorization_when_discovery_fails(auth_app, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from mcp_manager.runtime import GatewayError

    app, web, user = auth_app
    row = await app.state.catalog.create({"name": "Callback", "transport": "rest", "config": {
        "auth": {"type": "oauth", "scope": "user", "authorization_url": "https://auth.test/authorize",
                 "token_url": "https://auth.test/token"},
        "tools": [{"name": "read", "request": {"url": "https://api.test/read"}}]}})
    from mcp_manager.database import McpServer
    async with app.state.db.locked() as session:
        (await session.get(McpServer, row.id)).mode = "lazy"
    started = (await web.post("/api/v1/mcps/" + row.id + "/oauth/start")).json()
    nonce = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    async def exchange(auth, data):
        return {"access_token": "authorized", "expires_at": time.time() + 3600}
    async def refresh(*args, **kwargs):
        raise GatewayError("startup_timeout", "Discovery unavailable")
    monkeypatch.setattr(app.state.oauth, "exchange", exchange)
    monkeypatch.setattr(app.state.catalog, "refresh", refresh)
    response = await web.get("/api/v1/oauth/callback", params={"state": nonce, "code": "approved"})
    assert response.status_code == 303
    assert response.headers["location"].endswith("/#/mcps")
    assert (await app.state.oauth.credentials(row, user["id"]))["access_token"] == "authorized"
