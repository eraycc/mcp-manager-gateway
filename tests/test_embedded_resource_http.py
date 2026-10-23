"""Exercise native clients over the real gateway's modern and legacy HTTP paths."""
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_gateway import running_gateway  # noqa: F401


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_embedded_resource_native_http_roundtrip_and_cleanup(running_gateway, monkeypatch, mode):  # noqa: F811
    app, web, url, token, _row = running_gateway
    class Connection:
        async def call(self, name, arguments):
            return {"structuredContent": {"result": "actual body"}, "content": [
                {"type": "text", "text": "downloaded"},
                {"type": "resource", "resource": {
                    "uri": "fixture://file/body", "mimeType": "text/plain", "text": "actual body",
                }},
            ]}
    @asynccontextmanager
    async def connector(spec):
        yield Connection()
    monkeypatch.setattr(app.state.runtime, "connector", connector)
    headers = {"Authorization": "Bearer " + token}
    lease = None
    if mode == "2026-07-28":
        lease = (await web.post("/gateway/v1/leases", headers=headers)).json()["id"]
        headers["X-MCP-Manager-Lease"] = lease
    async with httpx2.AsyncClient(timeout=35, headers=headers, trust_env=False) as http:  # noqa: SIM117
        async with Client(streamable_http_client(url + "/mcp", http_client=http), cache=None,
                          mode=mode) as client:
            value = await client.call_tool("echo__echo", {"value": "request"})
            assert value.content[1].resource.text == "actual body"
            page = await client.list_resources()
            assert len(page.resources) == 1
            uri = str(page.resources[0].uri)
            result = await client.read_resource(uri)
            assert result.contents[0].text == "actual body"
    if lease:
        assert (await web.delete("/gateway/v1/leases/" + lease, headers=headers)).status_code == 200
    assert not app.state.gateway.embedded.items
