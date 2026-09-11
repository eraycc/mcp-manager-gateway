import httpx
import pytest
from mcp_manager.app import create_app
from mcp_manager.config import Settings

@pytest.mark.asyncio
async def test_mcp_crud_rest_cache_and_user_grants(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, database_url="", secret_key="test-key"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/api/v1/auth/register", json={"username":"admin","password":"password1234"})).status_code < 300
            assert (await client.post("/api/v1/auth/login", json={"username":"admin","password":"password1234"})).status_code == 200
            client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]
            config = {"tools":[{"name":"echo","inputSchema":{"type":"object"},"request":{"url":"https://example.test/echo"}}]}
            created = await client.post("/api/v1/mcps", json={"name":"REST Echo","slug":"rest_echo","transport":"rest","config":config})
            assert created.status_code < 300, created.text
            id = created.json()["id"]
            assert app.state.runtime.status() == []
            tools = (await client.post(f"/api/v1/mcps/{id}/refresh")).json()
            assert tools["tools"][0]["name"] == "echo"
            exported = (await client.post("/api/v1/mcps/export", json={"ids":[id]})).json()
            assert "rest_echo" in exported["mcpServers"]
            conflict = await client.patch(f"/api/v1/mcps/{id}", json={"revision":99,"name":"bad"})
            assert conflict.status_code == 409
            _token = (await client.post("/api/v1/tokens", json={"name":"all","scope_mode":"all"})).json()["token"]
            assert (await client.get("/api/v1/tokens")).json()["total"] == 1
            assert (await client.delete(f"/api/v1/mcps/{id}")).status_code < 300


@pytest.mark.asyncio
async def test_raw_log_does_not_store_authentication_secrets(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, database_url="", secret_key="test-key"))
    async with app.router.lifespan_context(app):
        await app.state.logs.append({"id":"x","status":"success","arguments":{"Authorization":"Bearer private"}})
        assert (await app.state.logs.detail("x"))["arguments"]["Authorization"] == "[REDACTED]"
