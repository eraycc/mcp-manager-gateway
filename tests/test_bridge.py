import asyncio
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from test_gateway import running_gateway  # noqa: F401, F811


async def test_stdio_bridge_releases_lease_and_shared_process(running_gateway):  # noqa: F811
    app, web, url, token, row = running_gateway
    source = StdioServerParameters(command=sys.executable,
        args=["-m", "mcp_manager.cli", "stdio", "--url", url],
        env={"MCP_MANAGER_TOKEN": token})
    async with Client(source, cache=None) as client:
        tools = await client.list_tools()
        assert any(t.name == "echo__echo" for t in tools.tools)
        result = await client.call_tool("echo__echo", {"value": "stdio bridge"})
        assert result.content[0].text == "stdio bridge"
        assert any(l.kind == "bridge" for l in app.state.runtime.leases.values())
    for _ in range(100):
        if not any(l.kind == "bridge" for l in app.state.runtime.leases.values()):
            break
        await asyncio.sleep(.05)
    assert not any(l.kind == "bridge" for l in app.state.runtime.leases.values())
    assert not any(r["phase"] == "ready" for r in app.state.runtime.status())
