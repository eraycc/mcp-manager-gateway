import asyncio
import socket

import pytest
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Mount, Route

from mcp_manager.runtime import Runtime, ServerSpec
from mcp_manager.transports import connect


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
async def test_real_network_transport_preserves_structured_and_image_content(transport):
    async def tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name="image", inputSchema={"type": "object"})])
    async def call(ctx, params):
        return types.CallToolResult(content=[types.TextContent(type="text", text="hello"),
            types.ImageContent(type="image", data="aGVsbG8=", mimeType="image/png")],
            structuredContent={"value": 42}, isError=False)
    protocol = Server("fixture", on_list_tools=tools, on_call_tool=call)
    if transport == "sse":
        sse = SseServerTransport("/messages/")
        class HandleSSE:
            async def __call__(self, scope, receive, send):
                async with sse.connect_sse(scope, receive, send) as streams:
                    await protocol.run(*streams, protocol.create_initialization_options())
        # The SDK sends ASGI frames itself; a Request endpoint would try to send
        # its None return as a second response when the SSE session ends.
        app = Starlette(routes=[Route("/sse", HandleSSE()), Mount("/messages/", app=sse.handle_post_message)])
        endpoint = "/sse"
    else:
        app = protocol.streamable_http_app()
        endpoint = "/mcp"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = "http://127.0.0.1:" + str(sock.getsockname()[1]) + endpoint
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
        await asyncio.sleep(.02)
    runtime = Runtime(connect)
    lease = runtime.create_lease("u", "t")
    spec = ServerSpec("remote", transport, {"url": url})
    try:
        cache = await runtime.discover(spec, lease.id)
        assert cache["tools"][0]["name"] == "image"
        result = await runtime.call(spec, lease.id, "image", {})
        assert result["structuredContent"] == {"value": 42}
        assert result["content"][1]["mimeType"] == "image/png"
        assert result["content"][1]["data"] == "aGVsbG8="
    finally:
        await runtime.release(lease.id)
        await runtime.close()
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()
