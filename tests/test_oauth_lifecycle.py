import asyncio
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import McpServer, get_setting
from mcp_manager.runtime import GatewayError


async def test_personal_oauth_pkce_refresh_and_disconnect_race(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path, secret_key="oauth-test-key"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as web:
            await web.post("/api/v1/auth/register", json={"username": "admin", "password": "password12345"})
            user = (await web.post("/api/v1/auth/login", json={"username": "admin", "password": "password12345"})).json()
            web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
            row = await app.state.catalog.create({"name": "OAuth REST", "transport": "rest",
                "isolation": "user",
                "config": {"auth": {"type": "oauth", "scope": "user", "authorization_url": "https://auth.test/authorize",
                    "token_url": "https://auth.test/token", "client_id": "client", "scopes": ["read"]},
                    "tools": [{"name": "read", "inputSchema": {"type": "object"},
                               "request": {"url": "https://api.test/value"}}]}},
                user_id=user["id"])
            response = await web.post("/api/v1/mcps/" + row.id + "/oauth/start")
            assert response.status_code == 200, response.text
            params = parse_qs(urlsplit(response.json()["authorization_url"]).query)
            assert params["code_challenge_method"] == ["S256"]
            assert len(params["code_challenge"][0]) == 43
            async def exchange(auth, data):
                assert data["grant_type"] == "authorization_code"
                assert len(data["code_verifier"]) >= 43
                return {"access_token": "access-user-1", "refresh_token": "refresh-1", "expires_at": time.time() + 3600}
            monkeypatch.setattr(app.state.oauth, "exchange", exchange)
            callback = await web.get("/api/v1/oauth/callback", params={"state": params["state"][0], "code": "code"})
            assert callback.status_code == 303, callback.text
            assert callback.headers["location"].endswith("/#/mcps")
            assert (await app.state.oauth.credentials(row, user["id"]))["access_token"] == "access-user-1"
            with pytest.raises(GatewayError):
                await app.state.oauth.credentials(row, "another-user")
            key = app.state.oauth.key(row, user["id"])
            async with app.state.db.locked() as session:
                saved = await session.get(McpServer, row.id)
                await app.state.credentials.save_oauth_token(
                    session, saved, user["id"],
                    {"access_token": "expired", "refresh_token": "refresh-1", "expires_at": 0},
                )
            entered, release = asyncio.Event(), asyncio.Event()
            async def refresh(auth, data):
                entered.set()
                await release.wait()
                return {"access_token": "new", "expires_at": time.time() + 3600}
            monkeypatch.setattr(app.state.oauth, "exchange", refresh)
            task = asyncio.create_task(app.state.oauth.credentials(row, user["id"]))
            await entered.wait()
            assert (await web.post("/api/v1/mcps/" + row.id + "/oauth/disconnect")).status_code == 200
            release.set()
            with pytest.raises(GatewayError):
                await task
            assert await get_setting(app.state.db, key) is None
            assert "oauth_token" not in await app.state.credentials.load(
                row.id, user["id"]
            )
