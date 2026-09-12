"""REST discovery probes connectivity without invoking business methods."""
import httpx
import pytest

from mcp_manager.runtime import GatewayError
from mcp_manager.transports import RestConnection


@pytest.mark.parametrize("status", [200, 204, 405])
async def test_rest_discovery_checks_connection_without_post(status):
    received = []
    def handle(request):
        received.append((request.method, str(request.url)))
        return httpx.Response(status)
    config = {"tools": [{"name": "create", "request": {"method": "POST", "url": "https://api.test/create"}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await RestConnection(config, client).discover()
    assert received == [("HEAD", "https://api.test/create")]
    assert result["tools"][0]["name"] == "create"


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
async def test_rest_unhealthy_status_cannot_be_ready(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status))) as client:
        with pytest.raises(GatewayError, match=str(status)):
            await RestConnection({"tools": [{"name": "x", "request": {"url": "https://api.test/x"}}]}, client).discover()


async def test_explicit_rest_health_url_used_for_templated_business_routes():
    seen = []
    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await RestConnection({"healthcheck_url": "https://api.test/health",
            "tools": [{"name": "x", "request": {"url": "https://api.test/items/{id}"}}]}, client).discover()
    assert seen == ["https://api.test/health"]


async def test_eager_rest_refresh_detects_server_that_stopped(tmp_path):
    from contextlib import asynccontextmanager
    from test_bug1_catalog import console
    online = True
    methods = []
    def handle(request):
        methods.append(request.method)
        if not online:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(405)
    @asynccontextmanager
    async def connect(spec):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            yield RestConnection(spec.config, client)
    async with console(tmp_path) as (app, web, actor):
        app.state.runtime.connector = connect
        response = await web.post("/api/v1/mcps", json={"name": "REST", "mode": "eager", "transport": "rest",
            "config": {"tools": [{"name": "create", "request": {"method": "POST", "url": "https://api.test/create"}}]}})
        row = response.json()
        assert row["mode"] == "eager" and row["tool_count"] == 1
        assert app.state.runtime.status()[0]["phase"] == "ready"
        online = False
        failed = await web.post("/api/v1/mcps/" + row["id"] + "/refresh")
        assert failed.status_code == 502
        current = (await web.get("/api/v1/mcps/" + row["id"])).json()
        assert current["mode"] == "disabled" and current["tool_count"] == 0
        assert current["auto_disabled"]
        assert "connection refused" in current["cache_error"]
        assert not app.state.runtime.instances
        assert set(methods) == {"HEAD"}
