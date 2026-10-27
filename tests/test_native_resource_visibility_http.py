"""Visibility changes reach existing clients on both MCP HTTP protocol eras."""
import asyncio
import sys
from contextlib import AsyncExitStack

import httpx2
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from test_gateway import running_gateway  # noqa: F401


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_visibility_change_notifies_existing_client(running_gateway, mode, transport):  # noqa: F811
    _app, web, url, token, _row = running_gateway
    received = asyncio.Queue()

    async def message_handler(message):
        root = getattr(message, "root", message)
        method = getattr(root, "method", None)
        if method in {"notifications/resources/list_changed", "notifications/prompts/list_changed"}:
            received.put_nowait(method)

    async with AsyncExitStack() as stack:
        if transport == "http":
            http = await stack.enter_async_context(httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + token}, trust_env=False))
            source = streamable_http_client(url + "/mcp", http_client=http)
        else:
            source = StdioServerParameters(command=sys.executable,
                args=["-m", "mcp_manager.cli", "stdio", "--url", url], env={"MCP_MANAGER_TOKEN": token})
        async with Client(source, mode=mode, cache=None, message_handler=message_handler) as client:
            await client.list_resources()
            if mode == "2026-07-28":
                async with client.listen(resources_list_changed=True, prompts_list_changed=True) as subscription:
                    for exposed in [False, True]:
                        response = await web.patch("/api/v1/settings", json={"exposeNativeResources": exposed})
                        assert response.status_code == 200
                        events = [await asyncio.wait_for(anext(subscription), 3) for _ in range(2)]
                        assert {type(event).__name__ for event in events} == {"ResourcesListChanged", "PromptsListChanged"}
                        assert (await client.list_resources()).resources == []
            else:
                for exposed in [False, True]:
                    response = await web.patch("/api/v1/settings", json={"exposeNativeResources": exposed})
                    assert response.status_code == 200
                    methods = {await asyncio.wait_for(received.get(), 3) for _ in range(2)}
                    assert methods == {"notifications/resources/list_changed", "notifications/prompts/list_changed"}
