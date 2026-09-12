"""Exercise OAuth scope adaptation through a real Streamable HTTP server."""
import asyncio
import socket

import httpx
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from starlette.responses import JSONResponse

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import set_setting


async def test_oauth_scope_refresh_and_call_over_http(tmp_path):
    received = []

    async def tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name="echo", inputSchema={"type": "object"})])

    async def call(ctx, params):
        return types.CallToolResult(content=[types.TextContent(type="text", text="ok")], isError=False)

    protocol = Server("oauth-fixture", on_list_tools=tools, on_call_tool=call)
    downstream = protocol.streamable_http_app()

    async def protected(scope, receive, send):
        if scope["type"] == "http":
            token = dict(scope["headers"]).get(b"authorization")
            received.append(token)
            if token != b"Bearer fixture-token":
                await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
                return
        await downstream(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = "http://127.0.0.1:" + str(sock.getsockname()[1]) + "/mcp"
    server = uvicorn.Server(uvicorn.Config(protected, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        app = create_app(Settings(data_dir=tmp_path, secret_key="network-scope"))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as web:
                await web.post("/api/v1/auth/register", json={"username": "admin", "password": "password12345"})
                user = (await web.post("/api/v1/auth/login",
                    json={"username": "admin", "password": "password12345"})).json()
                row = await app.state.catalog.create({"name": "HTTP OAuth", "transport": "streamable-http",
                    "config": {"url": url, "auth": {"type": "oauth", "scope": "user", "scopes": ["mcp:read"],
                        "authorization_url": "https://auth.test/authorize", "token_url": "https://auth.test/token"}}})
                await set_setting(app.state.db, app.state.oauth.key(row, user["id"]), app.state.catalog.seal(
                    {"access_token": "fixture-token", "scope": "mcp:read", "expires_at": 9999999999}))
                row = await app.state.catalog.update(row.id, {"mode": "lazy"}, user_id=user["id"])
                cached = await app.state.catalog.refresh(row.id, user["id"])
                assert cached["cache_status"] == "ready"
                assert cached["tools"][0]["name"] == "echo"
                spec = await app.state.catalog.spec(row, user["id"])
                lease = app.state.runtime.create_lease(user["id"], "test")
                try:
                    result = await app.state.runtime.call(spec, lease.id, "echo", {})
                    assert result["content"][0]["text"] == "ok"
                    assert not result["isError"]
                finally:
                    await app.state.runtime.release(lease.id)
                assert received and all(x == b"Bearer fixture-token" for x in received)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()
