import asyncio
import socket
import sys
from pathlib import Path

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_manager.app import create_app
from mcp_manager.config import Settings


@pytest.fixture
async def running_gateway(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    app = create_app(Settings(data_dir=tmp_path, database_url="", secret_key="test-key", public_url=url))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(300):
        if server.started:
            break
        if task.done():
            await task
        await asyncio.sleep(.02)
    assert server.started
    try:
        # Service creation includes discovery with a 30-second startup budget.
        async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=35) as web:
            response = await web.post("/api/v1/auth/register", json={"username": "admin", "password": "password12345"})
            assert response.status_code == 200
            await web.post("/api/v1/auth/login", json={"username": "admin", "password": "password12345"})
            web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
            fixture = str(Path(__file__).parent / "fixtures/echo_server.py")
            response = await web.post("/api/v1/mcps", json={"name": "Echo", "slug": "echo", "transport": "stdio",
                "config": {"command": sys.executable, "args": [fixture]}})
            assert response.status_code == 200, response.text
            row = response.json()
            assert app.state.runtime.status() == []
            # Creation already discovers and caches tools, then releases lazy
            # instances. Refresh behavior has its own catalog tests.
            assert row["cache_status"] == "ready"
            assert row["tool_count"] > 0
            response = await web.post("/api/v1/tokens", json={"name": "agent", "scope_mode": "all"})
            token = response.json()["token"]
            yield app, web, url, token, row
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()


@pytest.mark.asyncio
async def test_http_cached_directory_call_and_explicit_release(running_gateway):
    app, web, url, token, row = running_gateway
    headers = {"Authorization": "Bearer " + token}
    lease = (await web.post("/gateway/v1/leases", headers=headers)).json()["id"]
    headers["X-MCP-Manager-Lease"] = lease
    assert not any(r["phase"] == "ready" for r in app.state.runtime.status())
    async with httpx2.AsyncClient(headers=headers, trust_env=False) as http:
        async with Client(streamable_http_client(url + "/mcp", http_client=http), cache=None) as client:
            tools = await client.list_tools()
            assert "echo__echo" in [t.name for t in tools.tools]
            assert not any(r["phase"] == "ready" for r in app.state.runtime.status())
            result = await client.call_tool("echo__echo", {"value": "你好"})
            assert "你好" in result.content[0].text
            assert sum(r["phase"] == "ready" for r in app.state.runtime.status()) == 1
    response = await web.delete("/gateway/v1/leases/" + lease, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200, response.text
    assert not any(r["phase"] == "ready" for r in app.state.runtime.status())
    stats = (await web.get("/api/v1/dashboard")).json()
    assert stats["calls"] == 1 and stats["success"] == 1


@pytest.mark.asyncio
async def test_token_revocation_and_anonymous_range(running_gateway):
    app, web, url, token, row = running_gateway
    response = await web.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
    assert response.status_code == 401
    listed = (await web.get("/api/v1/tokens")).json()["items"]
    await web.patch("/api/v1/tokens/" + listed[0]["id"], json={"disabled": True})
    response = await web.post("/mcp", headers={"Authorization": "Bearer " + token}, json={})
    assert response.status_code == 401
    response = await web.patch("/api/v1/settings", json={"token_auth_enabled": False})
    assert response.status_code == 200, response.text
    async with httpx2.AsyncClient(trust_env=False) as http:
        async with Client(streamable_http_client(url + "/mcp", http_client=http), cache=None) as client:
            assert (await client.list_tools()).tools == []
