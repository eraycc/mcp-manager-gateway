"""Read resources/prompts/templates through real stateful HTTP and SSE peers."""
import asyncio
import socket
from types import SimpleNamespace

import pytest
import uvicorn
from mcp.server import MCPServer
from test_exact_route_scalability import gateway_context, invoke

from mcp_manager.database import ApiToken


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
async def test_resource_proxy_with_real_downstream_session(db_app, transport):
    peer = MCPServer("device-peer")

    @peer.resource("device://health")
    def health() -> str:
        return "healthy"

    @peer.resource("device://sensor/{key}")
    def sensor(key: str) -> str:
        return "sensor-" + key

    @peer.prompt()
    def status(key: str) -> str:
        return "status-" + key

    @peer.prompt()
    def workflow() -> str:
        return "issue-workflow"

    application = peer.sse_app() if transport == "sse" else peer.streamable_http_app(json_response=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = "http://127.0.0.1:" + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(300):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        assert server.started
        response = await db_app.web.post("/api/v1/mcps", json={
            "name": "Device HTTP", "slug": "devices", "transport": transport,
            "config": {"url": url + ("/sse" if transport == "sse" else "/mcp")},
        })
        assert response.status_code == 200, response.text
        row = response.json()
        refreshed = await db_app.web.post(f'/api/v1/mcps/{row["id"]}/refresh')
        assert refreshed.status_code == 200, refreshed.text
        ctx, identity = await gateway_context(db_app)
        async with db_app.db.locked() as session:
            (await session.get(ApiToken, identity)).enable_resource_tools = True
        uri = f'mcp-manager://{row["id"]}/device://health'
        value = await db_app.app.state.gateway.read_resource(ctx, SimpleNamespace(uri=uri))
        assert value.contents[0].text == "healthy"
        for _ in range(2):
            value = await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri})
            assert not value.is_error, value
            assert value.structured_content["contents"][0]["text"] == "healthy"
        template_uri = f'mcp-manager://{row["id"]}/device://sensor/battery'
        value = await db_app.app.state.gateway.read_resource(ctx, SimpleNamespace(uri=template_uri))
        assert value.contents[0].text == "sensor-battery"
        templates = await db_app.app.state.gateway.list_templates(ctx, SimpleNamespace(cursor=None))
        assert templates.resource_templates[0].uri_template == f'mcp-manager://{row["id"]}/device://sensor/{{key}}'
        prompt = await db_app.app.state.gateway.get_prompt(
            ctx, SimpleNamespace(name="devices__status", arguments={"key": "battery"}),
        )
        assert prompt.messages[0].content.text == "status-battery"
        workflow_uri = f'mcp-manager://{row["id"]}/prompt://devices/workflow'
        value = await db_app.app.state.gateway.read_resource(ctx, SimpleNamespace(uri=workflow_uri))
        assert '"text": "issue-workflow"' in value.contents[0].text
        value = await invoke(db_app, ctx, "gateway_read_resource", {"uri": workflow_uri})
        assert not value.is_error, value
        assert value.structured_content["messages"][0]["content"]["text"] == "issue-workflow"
    finally:
        # Release gateway-owned connections before the peer shuts down.
        for instance in db_app.app.state.runtime.status():
            await db_app.app.state.runtime.stop_server(instance["server_id"])
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()
