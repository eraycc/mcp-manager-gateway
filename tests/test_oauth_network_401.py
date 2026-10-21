"""Real HTTP/SSE 401 responses persist reauthorization and never replay calls."""
import asyncio
import json
import socket

import pytest
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
@pytest.mark.parametrize("rejected_method", ["tools/list", "tools/call"])
async def test_downstream_401_stops_at_reauthorization(db_app, transport, rejected_method):
    observed = []
    reject = {"method": "tools/list" if rejected_method == "tools/list" else None}

    async def tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name="echo", inputSchema={"type": "object"})])

    async def call(ctx, params):
        return types.CallToolResult(content=[types.TextContent(type="text", text="ok")])

    protocol = Server("oauth-401-fixture", on_list_tools=tools, on_call_tool=call)
    if transport == "sse":
        sse = SseServerTransport("/messages/")

        class HandleSSE:
            async def __call__(self, scope, receive, send):
                async with sse.connect_sse(scope, receive, send) as streams:
                    await protocol.run(*streams, protocol.create_initialization_options())

        downstream = Starlette(routes=[Route("/sse", HandleSSE()), Mount("/messages/", app=sse.handle_post_message)])
        endpoint = "/sse"
    else:
        downstream = protocol.streamable_http_app()
        endpoint = "/mcp"

    async def protected(scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            frames, body = [], bytearray()
            while True:
                frame = await receive()
                frames.append(frame)
                body.extend(frame.get("body", b""))
                if not frame.get("more_body"):
                    break
            message = json.loads(body) if body else {}
            method = message.get("method")
            observed.append(method)
            if method == reject["method"]:
                await JSONResponse({"error": "expired"}, 401)(scope, receive, send)
                return

            async def replay():
                return frames.pop(0) if frames else await receive()

            await downstream(scope, replay, send)
            return
        await downstream(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}{endpoint}"
    server = uvicorn.Server(uvicorn.Config(protected, log_level="error"))
    job = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if job.done():
                    await job
                await asyncio.sleep(.02)
        user = db_app.actor["id"]
        row = await db_app.app.state.catalog.create({
            "name": "401 fixture", "transport": transport, "isolation": "user",
            "config": {"url": url, "call_timeout": 3, "startup_timeout": 3, "stop_timeout": 2, "auth": {"type": "oauth", "auth_mode": "manual", "client_id": "public",
                "authorization_url": "https://auth.test/a", "token_url": "https://auth.test/t"}},
        }, user_id=user)
        async with db_app.db.locked() as session:
            await db_app.app.state.credentials.save_oauth_token(session, row, user, {
                "access_token": "old", "refresh_token": "do-not-refresh-after-401", "expires_at": 9999999999})
        spec = await db_app.app.state.catalog.spec(row, user)
        lease = db_app.app.state.runtime.create_lease(user, "test")
        try:
            if rejected_method == "tools/call":
                result = await db_app.app.state.runtime.discover(spec, lease.id)
                assert result["tools"][0]["name"] == "echo"
                reject["method"] = "tools/call"
            with pytest.raises(GatewayError) as error:
                if rejected_method == "tools/list":
                    await db_app.app.state.runtime.discover(spec, lease.id)
                else:
                    await db_app.app.state.runtime.call(spec, lease.id, "echo", {})
            assert error.value.code == "auth_required"
            payload = await db_app.app.state.credentials.load(row.id, user)
            assert payload["oauth_state"]["status"] == "needs_reauth"
            with pytest.raises(GatewayError):
                await db_app.app.state.catalog.spec(row, user)
            assert observed.count(rejected_method) == 1
        finally:
            await db_app.app.state.runtime.release(lease.id)
    finally:
        server.should_exit = True
        await asyncio.wait_for(job, 15)
        sock.close()
