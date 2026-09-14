"""Real HTTP and stdio bridge contract matrix with stdio and HTTP downstreams."""
import asyncio
import json
import socket
import sys
from contextlib import asynccontextmanager

import httpx2
import pytest
import uvicorn
from mcp import Client, types
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server

from test_gateway import running_gateway  # noqa: F401


@asynccontextmanager
async def remote_fixture():
    calls = []
    async def listing(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name="echo", description="Remote numerical echo",
            inputSchema={"type": "object", "properties": {"number": {"type": "integer"}},
                         "required": ["number"], "additionalProperties": False})])
    async def call(ctx, params):
        calls.append(params.arguments)
        return types.CallToolResult(content=[types.TextContent(type="text", text="remote")],
                                     structuredContent={"number": params.arguments["number"]})
    protocol = Server("remote-fixture", on_list_tools=listing, on_call_tool=call)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = "http://127.0.0.1:" + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(protocol.streamable_http_app(), log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(300):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.02)
        assert server.started
        yield url, calls
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()


@asynccontextmanager
async def gateway_client(transport, url, token):
    if transport == "stdio":
        source = StdioServerParameters(command=sys.executable,
            args=["-m", "mcp_manager.cli", "stdio", "--url", url],
            env={"MCP_MANAGER_TOKEN": token})
        async with Client(source, cache=None) as client:
            yield client
    else:
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as http:
            async with Client(streamable_http_client(url + "/mcp", http_client=http), cache=None) as client:
                yield client


@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("mode", ["native", "discovery"])
async def test_full_matrix_with_two_downstream_transports(running_gateway, transport, mode):
    app, web, url, _, local = running_gateway
    async with remote_fixture() as (remote_url, calls):
        response = await web.post("/api/v1/mcps", json={"name": "Remote", "slug": "remote",
            "transport": "streamable-http", "config": {"url": remote_url + "/mcp"}})
        assert response.status_code == 200, response.text
        remote = response.json()
        token = (await web.post("/api/v1/tokens", json={"name": mode, "scope_mode": "all",
                                                      "discovery_mode": mode})).json()["token"]
        async with gateway_client(transport, url, token) as client:
            listing = await client.list_tools()
            if mode == "native":
                catalog = {tool.name: tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools}
            else:
                assert {tool.name for tool in listing.tools} == {
                    "gateway_search_mcps", "gateway_search_tools", "gateway_call"}
                services = await client.call_tool("gateway_search_mcps", {"query": "*"})
                data = services.structured_content
                assert {item["slug"] for item in data["items"]} == {"echo", "remote"}
                by_slug = {item["slug"]: item for item in data["items"]}
                assert by_slug["echo"]["tools_list"] == ["echo", "structured"]
                assert by_slug["remote"]["tools_list"] == ["echo"]
                assert all(item["tool_count"] == len(item["tools_list"]) for item in data["items"])
                assert all("tools" not in item and "inputSchema" not in item for item in data["items"])
                catalog, cursor = {}, None
                while True:
                    found = await client.call_tool("gateway_search_tools", {
                        "mcp": "*", "tool": "*", "limit": 1, **({"cursor": cursor} if cursor else {})})
                    assert not found.is_error
                    assert json.loads(found.content[0].text) == found.structured_content
                    for item in found.structured_content["items"]:
                        assert item["gateway_name"] not in catalog
                        catalog[item["gateway_name"]] = item
                    cursor = found.structured_content["next_cursor"]
                    if not cursor:
                        break
                scoped = await client.call_tool("gateway_search_tools", {"mcp": remote["id"], "tool": ""})
                assert [i["gateway_name"] for i in scoped.structured_content["items"]] == ["remote__echo"]
                by_name = await client.call_tool("gateway_search_tools", {"mcp": "", "tool": "echo"})
                assert {i["gateway_name"] for i in by_name.structured_content["items"]} == {
                    "echo__echo", "remote__echo"}
                native_token = (await web.post("/api/v1/tokens", json={"name": "native-reference",
                                                                     "scope_mode": "all"})).json()["token"]
                async with gateway_client("http", url, native_token) as reference:
                    original = await reference.list_tools()
                    for item in original.tools:
                        assert catalog[item.name]["inputSchema"] == item.model_dump(by_alias=True)["inputSchema"]
            assert set(catalog) == {"echo__echo", "echo__structured", "remote__echo"}
            assert not any(x["phase"] == "ready" for x in app.state.runtime.status())

            async def invoke(name, arguments):
                return await client.call_tool("gateway_call", {"name": name, "arguments": arguments}) if (
                    mode == "discovery") else await client.call_tool(name, arguments)
            bad = await invoke("remote__echo", {"value": "wrong schema"})
            assert bad.is_error and calls == []
            local_result = await invoke("echo__echo", {"value": "中文"})
            assert local_result.content[0].text == "中文"
            remote_result = await invoke("remote__echo", {"number": 7})
            assert remote_result.structured_content == {"number": 7}
            assert calls == [{"number": 7}]
