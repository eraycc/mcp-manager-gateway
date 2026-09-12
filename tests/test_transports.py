import json
import sys
from pathlib import Path
import httpx
import pytest
from mcp_manager.runtime import ServerSpec, Runtime
from mcp_manager.transports import RestConnection, connect


@pytest.mark.asyncio
async def test_rest_preserves_types_and_maps_response():
    def handle(request):
        assert request.url.path == "/items/hello%20world" or request.url.path == "/items/hello world"
        assert json.loads(request.content) == {"active": True, "count": 3}
        return httpx.Response(200, json={"result": {"id": 9, "ok": True}})
    config = {"tools": [{"name": "create", "inputSchema": {"type": "object"},
              "request": {"method": "POST", "url": "https://example.test/items/{name}",
                          "json": {"active": "{active}", "count": "{count}"}},
              "response": {"pointer": "/result"}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await RestConnection(config, client).call("create", {"name": "hello world", "active": True, "count": 3})
    assert result["structuredContent"] == {"id": 9, "ok": True}
    assert not result.get("isError", False)


@pytest.mark.asyncio
async def test_rest_http_error_is_tool_error():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(409, json={"error": "conflict"}))) as client:
        connection = RestConnection({"tools": [{"name": "x", "request": {"url": "https://example.test/x"}}]}, client)
        result = await connection.call("x", {})
        assert result["isError"] is True


@pytest.mark.asyncio
async def test_real_stdio_fixture():
    runtime = Runtime(connect)
    lease = runtime.create_lease("u", "t")
    fixture = Path(__file__).parent / "fixtures" / "echo_server.py"
    server = ServerSpec(id="echo", transport="stdio", config={"command": sys.executable, "args": [str(fixture)], "startup_timeout": 20})
    catalog = await runtime.discover(server, lease.id)
    assert any(t["name"] == "echo" for t in catalog["tools"])
    result = await runtime.call(server, lease.id, "echo", {"value": "你好"})
    assert result["content"][0]["text"] == "你好"
    await runtime.release(lease.id)
    assert runtime.status() == []
    await runtime.close()
