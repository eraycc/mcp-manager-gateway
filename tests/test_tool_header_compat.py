"""Cold downstream connections must mirror cached schema annotations on HTTP calls."""
import asyncio
import socket

import pytest
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from starlette.middleware.base import BaseHTTPMiddleware
from test_catalog_paged_api import indexed_catalog


@pytest.mark.parametrize(("owner", "mirror"), [("example", False), ("example", True),
                                              ("中文 owner", True), (" line\nbreak ", True)])
async def test_manual_test_cold_connection_mirrors_headers(db_app, monkeypatch, owner, mirror):
    schema = {"type": "object", "properties": {
        "owner": {"type": "string", "x-mcp-header": "Owner"},
        "options": {"type": "object", "properties": {
            "repo": {"type": "string", "x-mcp-header": "Repository"},
        }},
    }, "required": ["owner", "options"]}
    listed, received = [], []
    async def tools(ctx, params):
        listed.append(True)
        return types.ListToolsResult(tools=[types.Tool(name="fetch", inputSchema=schema)])
    async def call(ctx, params):
        received.append(params.arguments)
        return types.CallToolResult(content=[types.TextContent(type="text", text="ok")])
    peer = Server("headers-peer", on_list_tools=tools, on_call_tool=call)
    application = peer.streamable_http_app(json_response=True)
    requests = []
    async def record(request, call_next):
        if request.method == "POST":
            requests.append(((await request.json()).get("method"), dict(request.headers)))
        return await call_next(request)
    application.add_middleware(BaseHTTPMiddleware, dispatch=record)
    if not mirror:
        import mcp_manager.tool_headers as header_module
        async def noop(request):
            pass
        monkeypatch.setattr(header_module, "mirror_tool_headers", noop)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = "http://127.0.0.1:" + str(sock.getsockname()[1]) + "/mcp"
    server = uvicorn.Server(uvicorn.Config(application, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
        await asyncio.sleep(.01)
    try:
        row, _ = await indexed_catalog(db_app, 0)
        from mcp_manager.database import McpServer
        async with db_app.db.locked() as session:
            value = await session.get(McpServer, row.id)
            value.transport = "streamable-http"
            value.config = db_app.app.state.catalog.seal({"url": url})
        row = await db_app.app.state.catalog.get(row.id)
        catalog = db_app.app.state.catalog
        scope = await catalog.index_scope(row)
        generation = await catalog.index.begin_refresh(scope)
        await catalog.index.stage_page(generation.id, "tools", [{"name": "fetch", "inputSchema": schema}])
        await catalog.index.publish(generation.id, expected_scope=scope)
        arguments = {"owner": owner, "options": {"repo": "repo-name"}}
        response = await db_app.web.post(f"/api/v1/mcps/{row.id}/test",
                                         json={"tool": "fetch", "arguments": arguments})
        call_index = next(index for index, (method, _) in enumerate(requests) if method == "tools/call")
        assert "tools/list" not in [method for method, _ in requests[:call_index]]
        assert sum(method == "tools/call" for method, _ in requests) == 1
        if mirror:
            assert response.status_code == 200, response.text
            assert not response.json().get("isError"), response.text
            assert received == [arguments]
            assert "mcp-param-owner" in requests[call_index][1]
            assert requests[call_index][1]["mcp-param-repository"] == "repo-name"
        else:
            assert response.status_code == 502, response.text
            assert response.json()["code"] == "downstream_error"
            assert "Mcp-Param-" in response.json()["detail"] and "missing" in response.json()["detail"]
            assert received == []
    finally:
        await db_app.app.state.runtime.close()
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()
