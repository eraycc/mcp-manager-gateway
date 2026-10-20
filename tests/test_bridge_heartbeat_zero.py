"""Zero disables Bridge liveness checks, while renewal and lease TTL stay independent."""
import asyncio
from types import SimpleNamespace

import pytest

from mcp_manager import bridge
from mcp_manager.runtime import Runtime


@pytest.mark.parametrize("existing", [False, True])
async def test_zero_disables_bridge_detection_for_new_and_existing_leases(existing):
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0], connection_ttl_seconds=0,
                      bridge_heartbeat_seconds=90 if existing else 0)
    lease = runtime.create_lease("alice", "token", kind="bridge")
    runtime.bridge_heartbeat_seconds = 0
    ticks[0] = 1000
    assert runtime.check_lease(lease.id) is lease
    row = runtime.lease_status()[0]
    assert row["bridge_heartbeat_seconds"] == 0
    assert row["bridge_missed_heartbeats"] is None and row["bridge_disconnect_at"] is None
    await runtime.reap()
    assert lease.id in runtime.leases


async def test_zero_does_not_disable_connection_lease_expiry():
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0], connection_ttl_seconds=10, bridge_heartbeat_seconds=0)
    lease = runtime.create_lease("alice", "token", kind="bridge")
    ticks[0] = 9
    assert runtime.check_lease(lease.id) is lease
    ticks[0] = 10
    await runtime.reap()
    assert lease.id not in runtime.leases


async def test_reenable_uses_renewal_window_until_next_heartbeat_then_new_policy():
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0], connection_ttl_seconds=0, bridge_heartbeat_seconds=0)
    lease = runtime.create_lease("alice", "token", kind="bridge")
    ticks[0] = 250
    runtime.heartbeat(lease.id, "alice", "token")
    runtime.bridge_heartbeat_seconds = 10
    ticks[0] = 339
    assert runtime.check_lease(lease.id) is lease
    runtime.heartbeat(lease.id, "alice", "token")
    assert runtime.lease_status()[0]["bridge_disconnect_at"] == pytest.approx(lease.created + 369)
    ticks[0] = 369
    await runtime.reap()
    assert lease.id not in runtime.leases


async def test_reenable_still_reaps_disconnected_previously_disabled_bridges():
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0], connection_ttl_seconds=0, bridge_heartbeat_seconds=0)
    lease = runtime.create_lease("alice", "token", kind="bridge")
    ticks[0] = 271
    runtime.bridge_heartbeat_seconds = 10
    await runtime.reap()
    assert lease.id not in runtime.leases


async def test_zero_setting_persists_and_wire_interval_stays_positive(db_app):
    web = db_app.web
    response = await web.patch("/api/v1/settings", json={"bridge_heartbeat_seconds": 0})
    assert response.status_code == 200, response.text
    assert (await web.get("/api/v1/settings")).json()["bridge_heartbeat_seconds"] == 0
    token = (await web.post("/api/v1/tokens", json={"name": "bridge-zero", "scope_mode": "all"})).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    data = (await web.post("/gateway/v1/leases", headers=headers)).json()
    assert data["heartbeat_seconds"] == 90 and data["heartbeat_detection_enabled"] is False
    heartbeat = (await web.post("/gateway/v1/leases/" + data["id"] + "/heartbeat", headers=headers)).json()
    assert heartbeat["heartbeat_seconds"] == 90 and heartbeat["heartbeat_detection_enabled"] is False
    row = (await web.get("/api/v1/leases/" + data["id"])).json()
    assert row["bridge_heartbeat_seconds"] == 0 and row["bridge_disconnect_at"] is None
    assert row["expires_at"] is not None


async def test_client_disabled_detection_keeps_bounded_renewals_and_recovers_policy(monkeypatch):
    sleeps = []
    responses = [{"heartbeat_seconds": 90, "heartbeat_detection_enabled": False},
                 {"heartbeat_seconds": 10, "heartbeat_detection_enabled": True}]
    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError
    class HTTP:
        async def post(self, path):
            return bridge.httpx.Response(200, json=responses.pop(0),
                request=bridge.httpx.Request("POST", "http://test" + path))
    monkeypatch.setattr(bridge, "asyncio", SimpleNamespace(sleep=sleep, Queue=asyncio.Queue))
    client = bridge.BridgeUpstream("http://test", "")
    with pytest.raises(asyncio.CancelledError):
        await client.heartbeat(HTTP(), "lease", 0)
    assert sleeps == [90, 90, 10] and not client.stale


async def test_client_ignores_heartbeat_failure_threshold_while_detection_disabled(monkeypatch):
    sleeps = []
    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 5:
            raise asyncio.CancelledError
    class HTTP:
        async def post(self, _path):
            raise bridge.httpx.ConnectError("offline")
    monkeypatch.setattr(bridge, "asyncio", SimpleNamespace(sleep=sleep, Queue=asyncio.Queue))
    client = bridge.BridgeUpstream("http://test", "")
    with pytest.raises(asyncio.CancelledError):
        await client.heartbeat(HTTP(), "lease", 90, 3, detection_enabled=False)
    assert len(sleeps) == 5 and not client.stale
