from contextlib import asynccontextmanager

import httpx
import pytest

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.runtime import GatewayError


@asynccontextmanager
async def console(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="bug1"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as web:
            await web.post("/api/v1/auth/register", json={"username": "admin", "password": "password12345"})
            actor = (await web.post("/api/v1/auth/login", json={"username": "admin", "password": "password12345"})).json()
            web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
            yield app, web, actor


class Connection:
    def __init__(self, tool="first"):
        self.tool = tool

    async def discover(self):
        return {"tools": [{"name": self.tool, "inputSchema": {"type": "object", "required": ["value"]}}],
                "resources": [], "templates": [], "prompts": []}

    async def call(self, name, arguments):
        return {"content": [{"type": "text", "text": "ok"}]}


async def test_create_edit_disable_refreshes_directory_without_kept_lazy_lease(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        @asynccontextmanager
        async def connect(spec):
            yield Connection(spec.config["command"])
        app.state.runtime.connector = connect
        response = await web.post("/api/v1/mcps", json={"name": "service", "config": {"command": "first"}})
        row = response.json()
        assert row["tool_count"] == 1
        assert not app.state.runtime.leases
        response = await web.patch("/api/v1/mcps/" + row["id"], json={"config": {"command": "second"}})
        assert response.json()["tool_count"] == 1
        tools = (await web.get("/api/v1/mcps/" + row["id"] + "/tools")).json()
        assert [t["name"] for t in tools["tools"]] == ["second"]
        response = await web.patch("/api/v1/mcps/" + row["id"], json={"mode": "disabled"})
        assert response.json()["cache_status"] == "disabled"
        assert (await web.get("/api/v1/mcps/" + row["id"] + "/tools")).json()["tools"] == []


async def test_admin_uses_own_personal_oauth_cache(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        row = await app.state.catalog.create({"name": "personal", "transport": "streamable-http",
            "config": {"url": "https://mcp.test", "auth": {"type": "oauth", "scope": "user",
                "authorization_url": "https://auth.test/authorize", "token_url": "https://auth.test/token"}}})
        from mcp_manager.database import set_setting
        await set_setting(app.state.db, app.state.oauth.key(row, actor["id"]),
                          app.state.catalog.seal({"access_token": "valid"}))
        @asynccontextmanager
        async def connect(spec):
            yield Connection()
        app.state.runtime.connector = connect
        row = await app.state.catalog.update(row.id, {"mode": "lazy"}, user_id=actor["id"])
        app.state.catalog.save_cache(row, {"tools": [{"name": "mine"}], "cache_at": "2026-09-12T00:00:00+00:00"}, actor["id"])
        app.state.catalog.save_cache(row, {"tools": [{"name": "other"}, {"name": "private"}]}, "other-user")
        item = (await web.get("/api/v1/mcps")).json()["items"][0]
        assert item["tool_count"] == 1
        assert item["cache_at"] == "2026-09-12T00:00:00+00:00"
        assert item["auth_type"] == "oauth"


async def test_personal_auth_failure_preserves_policy_and_authorization_reason(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        row = await app.state.catalog.create({"name": "oauth", "transport": "streamable-http",
            "config": {"url": "https://mcp.test", "auth": {"type": "oauth",
                "authorization_url": "https://auth.test/authorize", "token_url": "https://auth.test/token"}}})
        assert row.mode == "lazy"
        with pytest.raises(GatewayError, match="OAuth"):
            await app.state.catalog.refresh(row.id)
        cached = app.state.catalog.cached(row)
        assert cached["cache_status"] == "auth_required"
        assert not cached.get("auto_disabled", False)
        assert cached["tools"] == []
        assert cached["cache_error_code"] == "auth_required"


async def test_missing_cached_tool_cannot_bypass_validation(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        @asynccontextmanager
        async def connect(spec):
            yield Connection()
        app.state.runtime.connector = connect
        row = await app.state.catalog.create({"name": "service", "config": {"command": "fake"}})
        app.state.catalog.save_cache(row, {"tools": []})
        lease = app.state.runtime.create_lease("system", "test")
        try:
            with pytest.raises(GatewayError) as exc:
                await app.state.catalog.call(row, lease, "missing", {})
            assert exc.value.code == "tool_not_found"
            with pytest.raises(GatewayError) as current:
                await app.state.catalog.call(row, lease, "first", {})
            assert current.value.code == "tool_not_found"
        finally:
            await app.state.runtime.release(lease.id)


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_eager_start_and_call_keeps_refresh_warning_without_implicit_discovery(tmp_path, transport):
    async with console(tmp_path) as (app, web, actor):
        @asynccontextmanager
        async def connect(spec):
            yield Connection("current")
        app.state.runtime.connector = connect
        config = {"command": "fake"} if transport == "stdio" else {"url": "https://mcp.test"}
        row = await app.state.catalog.create({"name": "eager", "mode": "eager", "transport": transport, "config": config})
        assert app.state.runtime.status()[0]["phase"] == "ready"
        assert app.state.catalog.cached(row)["tools"][0]["name"] == "current"
        await app.state.runtime.stop_server(row.id)
        app.state.catalog.save_cache(row, {
            "tools": [{"name": "current", "inputSchema": {"type": "object"}}],
            "cache_status": "ready",
            "last_refresh_error": "old tools/list failure",
            "last_refresh_error_code": "connection_error",
        })
        lease = app.state.runtime.create_lease("system", "test")
        try:
            await app.state.catalog.call(row, lease, "current", {"value": 1})
            assert app.state.catalog.cached(row)["last_refresh_error"] == "old tools/list failure"
            assert app.state.catalog.cached(row)["cache_status"] == "ready"
        finally:
            await app.state.runtime.release(lease.id)


async def test_revoked_credential_discovery_does_not_restore_cache(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        row = await app.state.catalog.create({"name": "personal", "transport": "streamable-http",
            "config": {"url": "https://mcp.test", "auth": {"type": "oauth", "scope": "user",
                "authorization_url": "https://auth.test/authorize", "token_url": "https://auth.test/token"}}})
        from mcp_manager.runtime import ServerSpec
        spec = ServerSpec(row.id, row.transport, {}, credential_owner=actor["id"], credential_scope="old")
        app.state.runtime.credential_versions[(row.id, actor["id"])] = "old"
        class RevokedConnection(Connection):
            async def discover(self):
                app.state.runtime.credential_versions[(row.id, actor["id"])] = None
                return await super().discover()
        with pytest.raises(GatewayError):
            result = await RevokedConnection().discover()
            await app.state.catalog.store_discovery(row, result, actor["id"], spec=spec)
        assert app.state.catalog.cached(row, actor["id"])["tools"] == []


async def test_failed_write_does_not_leave_a_runtime_hold(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        row = await app.state.catalog.create({"name": "service", "mode": "disabled", "config": {"command": "fake"}})
        async def reject_transaction(session=None):
            if session is not None:
                from fastapi import HTTPException
                raise HTTPException(403, "revoked")
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            await app.state.catalog.update(row.id, {"name": "changed"}, authorize=reject_transaction)
        assert row.id not in app.state.runtime.holds
        with pytest.raises(HTTPException):
            await app.state.catalog.delete(row.id, authorize=reject_transaction)
        assert row.id not in app.state.runtime.holds


async def test_browser_gateway_cors_and_management_origin_are_separate(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        preflight = {"Origin": "https://client.test", "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "authorization,content-type,mcp-protocol-version"}
        response = await web.options("/mcp", headers=preflight)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in response.headers
        token = (await web.post("/api/v1/tokens", json={"name": "browser", "scope_mode": "all"})).json()["token"]
        response = await web.post("/mcp", headers={"Origin": "https://client.test", "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert response.status_code == 200, response.text
        assert response.headers["access-control-allow-origin"] == "*"
        assert (await web.patch("/api/v1/settings", headers={"Origin": "https://client.test"},
                               json={"title": "forbidden"})).status_code == 403
        response = await web.patch("/api/v1/settings", json={"cors_origins": ["https://allowed.test"]})
        assert response.status_code == 200, response.text
        assert (await web.options("/mcp", headers=preflight)).status_code == 403
        assert (await web.options("/mcp", headers=preflight | {"Origin": "https://allowed.test"})).status_code == 200
