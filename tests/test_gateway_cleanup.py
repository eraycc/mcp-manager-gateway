import asyncio
import time

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_gateway import running_gateway  # noqa: F401

from mcp_manager import bridge
from mcp_manager.bridge import BridgeUpstream


async def test_idle_session_reap_uses_config_and_closes_sdk_session(running_gateway):  # noqa: F811
    app, _web, url, token, _ = running_gateway
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as http:  # noqa: SIM117 - MCP client depends on HTTP context
        async with Client(streamable_http_client(url + "/mcp", http_client=http), cache=None, mode="legacy") as client:
            await client.list_tools()
            sid = next(iter(app.state.gateway.sessions))
            app.state.gateway.sessions[sid]["touched"] = time.monotonic() - 10
            app.state.runtime.idle_seconds = 0
            await app.state.gateway.reap()
            assert sid in app.state.gateway.sessions
            app.state.runtime.idle_seconds = 1
            await app.state.gateway.reap()
            assert sid not in app.state.gateway.sessions
            assert sid not in app.state.protocol.session_manager._server_instances


async def test_bridge_expired_lease_recovers_next_call_and_cleans_old_session(running_gateway, monkeypatch):  # noqa: F811
    app, _web, url, token, _ = running_gateway
    monkeypatch.setattr(bridge, "Client", lambda *args, **kwargs: Client(*args, **kwargs, mode="legacy"))
    async with BridgeUpstream(url, token) as upstream:
        assert (await upstream.request("call_tool", "echo__echo", {"value": "first"})).content[0].text == "first"
        old = next(l for l in app.state.runtime.leases.values() if l.kind == "bridge")
        old.touched -= 100
        # The first uncertain request is returned as an error, never replayed.
        with pytest.raises(Exception):  # noqa: B017 - SDK transport exception type is not stable
            await upstream.request("list_tools", cache_mode="bypass")
        assert (await upstream.request("call_tool", "echo__echo", {"value": "second"})).content[0].text == "second"
        assert old.id not in app.state.runtime.leases
        assert len(app.state.gateway.sessions) == 1
    await asyncio.sleep(.05)
    assert not app.state.gateway.sessions


async def test_expired_token_prunes_session_after_lease_was_reaped(running_gateway):  # noqa: F811
    from datetime import timedelta

    from mcp_manager.database import ApiToken, now

    app, web, url, token, _ = running_gateway
    token_id = (await web.get("/api/v1/tokens")).json()["items"][0]["id"]
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as http:  # noqa: SIM117 - MCP client depends on HTTP context
        async with Client(streamable_http_client(url + "/mcp", http_client=http), cache=None, mode="legacy") as client:
            await client.list_tools()
            sid = next(iter(app.state.gateway.sessions))
            await app.state.runtime.release(app.state.gateway.sessions[sid]["lease"])
            async with app.state.db.locked() as session:
                row = await session.get(ApiToken, token_id)
                row.expires_at = now() - timedelta(seconds=1)
            await app.state.gateway.reap()
            assert sid not in app.state.gateway.sessions
            assert sid not in app.state.protocol.session_manager._server_instances


async def test_reap_does_not_revoke_session_created_during_permission_query(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from mcp_manager.gateway import Gateway
    from mcp_manager.runtime import Runtime

    runtime = Runtime(None)
    state = SimpleNamespace(runtime=runtime)
    gateway = Gateway(SimpleNamespace(state=state))
    def bound(user, token):
        lease = runtime.create_lease(user, token, kind="retention")
        return {"owner": (user, token), "lease": lease.id, "touched": time.monotonic()}
    gateway.sessions["existing"] = bound("user-a", "token-a")
    class DB:
        @asynccontextmanager
        async def session(self):
            yield self
        async def execute(self, statement):
            # A new authorized connection arrives while the older snapshot is queried.
            gateway.sessions["new"] = bound("user-b", "token-b")
            return [(SimpleNamespace(id="token-a", disabled=False, expires_at=None),
                     SimpleNamespace(id="user-a", disabled=False))]
    state.db = DB()
    closed = []
    async def close(sid):
        closed.append(sid)
        gateway.sessions.pop(sid, None)
    monkeypatch.setattr(gateway, "close_session", close)
    await gateway.reap()
    assert closed == []
    assert set(gateway.sessions) == {"existing", "new"}


@pytest.mark.parametrize("explicit", [False, True])
async def test_web_disconnect_removes_actual_sdk_session_and_runtime_refs(running_gateway, explicit):  # noqa: F811
    app, web, url, token, _ = running_gateway
    headers = {"Authorization": "Bearer " + token}
    lease_id = None
    if explicit:
        response = await web.post("/gateway/v1/leases", headers=headers)
        lease_id = response.json()["id"]
        headers["X-MCP-Manager-Lease"] = lease_id
    async with httpx2.AsyncClient(headers=headers, trust_env=False) as http:  # noqa: SIM117 - MCP client depends on HTTP context
        async with Client(streamable_http_client(url + "/mcp", http_client=http), cache=None, mode="legacy") as client:
            await client.call_tool("echo__echo", {"value": "connection-management"})
            sid = next(iter(app.state.gateway.sessions))
            bound = app.state.gateway.sessions[sid]
            selected = lease_id or bound["lease"]
            response = await web.post("/api/v1/leases/" + selected + "/disconnect")
            assert response.status_code == 200, response.text
            assert response.json()["ok"]
            assert sid not in app.state.gateway.sessions
            assert sid not in app.state.protocol.session_manager._server_instances
            assert selected not in app.state.runtime.leases
            assert bound["lease"] not in app.state.runtime.leases
            assert not app.state.runtime.instances
