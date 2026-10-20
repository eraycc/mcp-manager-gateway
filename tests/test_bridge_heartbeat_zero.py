"""A zero missed-heartbeat limit disables detection; interval and lease TTL are independent."""
import asyncio
from types import SimpleNamespace

import pytest

from mcp_manager import bridge
from mcp_manager.runtime import Runtime


@pytest.mark.parametrize("existing", [False, True])
async def test_zero_disables_bridge_detection_for_new_and_existing_leases(existing):
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0], connection_ttl_seconds=0,
                      bridge_missed_heartbeats=3 if existing else 0)
    lease = runtime.create_lease("alice", "token", kind="bridge")
    runtime.bridge_missed_heartbeats = 0
    ticks[0] = 1000
    assert runtime.check_lease(lease.id) is lease
    row = runtime.lease_status()[0]
    assert row["bridge_heartbeat_seconds"] == 90
    assert row["bridge_heartbeat_detection_enabled"] is False
    assert row["bridge_missed_heartbeats"] is None and row["bridge_disconnect_at"] is None
    await runtime.reap()
    assert lease.id in runtime.leases


async def test_zero_does_not_disable_connection_lease_expiry():
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0], connection_ttl_seconds=10, bridge_missed_heartbeats=0)
    lease = runtime.create_lease("alice", "token", kind="bridge")
    ticks[0] = 9
    assert runtime.check_lease(lease.id) is lease
    ticks[0] = 10
    await runtime.reap()
    assert lease.id not in runtime.leases


async def test_reenable_uses_renewal_window_until_next_heartbeat_then_new_policy():
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0], connection_ttl_seconds=0, bridge_missed_heartbeats=0)
    lease = runtime.create_lease("alice", "token", kind="bridge")
    ticks[0] = 250
    runtime.heartbeat(lease.id, "alice", "token")
    runtime.bridge_heartbeat_seconds = 10
    runtime.bridge_missed_heartbeats = 3
    ticks[0] = 339
    assert runtime.check_lease(lease.id) is lease
    runtime.heartbeat(lease.id, "alice", "token")
    assert runtime.lease_status()[0]["bridge_disconnect_at"] == pytest.approx(lease.created + 369)
    ticks[0] = 369
    await runtime.reap()
    assert lease.id not in runtime.leases


async def test_reenable_still_reaps_disconnected_previously_disabled_bridges():
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0], connection_ttl_seconds=0, bridge_missed_heartbeats=0)
    lease = runtime.create_lease("alice", "token", kind="bridge")
    ticks[0] = 271
    runtime.bridge_heartbeat_seconds = 10
    runtime.bridge_missed_heartbeats = 3
    await runtime.reap()
    assert lease.id not in runtime.leases


async def test_zero_setting_persists_and_wire_interval_stays_positive(db_app):
    web = db_app.web
    response = await web.patch("/api/v1/settings", json={"bridge_heartbeat_seconds": 60, "bridge_missed_heartbeats": 0})
    assert response.status_code == 200, response.text
    settings = (await web.get("/api/v1/settings")).json()
    assert settings["bridge_heartbeat_seconds"] == 60 and settings["bridge_missed_heartbeats"] == 0
    assert (await web.patch("/api/v1/settings", json={"bridge_heartbeat_seconds": 0})).status_code == 422
    token = (await web.post("/api/v1/tokens", json={"name": "bridge-zero", "scope_mode": "all"})).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    data = (await web.post("/gateway/v1/leases", headers=headers)).json()
    assert data["heartbeat_seconds"] == 60 and data["heartbeat_detection_enabled"] is False
    heartbeat = (await web.post("/gateway/v1/leases/" + data["id"] + "/heartbeat", headers=headers)).json()
    assert heartbeat["heartbeat_seconds"] == 60 and heartbeat["heartbeat_detection_enabled"] is False
    row = (await web.get("/api/v1/leases/" + data["id"])).json()
    assert row["bridge_heartbeat_seconds"] == 60 and row["bridge_disconnect_at"] is None
    assert row["expires_at"] is not None


async def test_client_disabled_detection_keeps_bounded_renewals_and_recovers_policy(monkeypatch):
    sleeps = []
    responses = [{"heartbeat_seconds": 60, "missed_heartbeats": 0, "heartbeat_detection_enabled": False},
                 {"heartbeat_seconds": 10, "missed_heartbeats": 3, "heartbeat_detection_enabled": True}]
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
    assert sleeps == [90, 60, 10] and not client.stale


@pytest.mark.parametrize(("missed", "detection_enabled"), [(0, True), (3, False)])
async def test_client_ignores_heartbeat_failure_threshold_while_detection_disabled(monkeypatch, missed, detection_enabled):
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
        await client.heartbeat(HTTP(), "lease", 90, missed, detection_enabled=detection_enabled)
    assert len(sleeps) == 5 and not client.stale


@pytest.mark.parametrize("update", [{"bridge_missed_heartbeats": 4}, {"title": "after legacy"}])
async def test_legacy_zero_interval_is_normalized_and_can_reenable_detection(db_app, update):
    from mcp_manager.database import get_setting, set_setting

    await set_setting(db_app.db, "bridge_heartbeat_seconds", 0)
    await set_setting(db_app.db, "bridge_missed_heartbeats", 3)
    settings = (await db_app.web.get("/api/v1/settings")).json()
    assert settings["bridge_heartbeat_seconds"] == 90
    assert settings["bridge_missed_heartbeats"] == 0
    response = await db_app.web.patch("/api/v1/settings", json=update)
    assert response.status_code == 200, response.text
    assert response.json()["bridge_heartbeat_seconds"] == 90
    assert response.json()["bridge_missed_heartbeats"] == update.get("bridge_missed_heartbeats", 0)
    assert await get_setting(db_app.db, "bridge_heartbeat_seconds") == 90
    assert db_app.app.state.runtime.bridge_heartbeat_seconds == 90
    assert db_app.app.state.runtime.bridge_missed_heartbeats == update.get("bridge_missed_heartbeats", 0)


@pytest.mark.parametrize("interval", [0, 60])
async def test_startup_preserves_disabled_detection_for_legacy_zero_interval(db_settings, interval):
    from mcp_manager.app import create_app
    from mcp_manager.database import Database, set_setting

    db = Database(db_settings)
    try:
        await db.initialize()
        await set_setting(db, "bridge_heartbeat_seconds", interval)
        await set_setting(db, "bridge_missed_heartbeats", 0 if interval else 3)
    finally:
        await db.close()
    app = create_app(db_settings)
    async with app.router.lifespan_context(app):
        assert app.state.runtime.bridge_heartbeat_seconds == (interval or 90)
        assert app.state.runtime.bridge_missed_heartbeats == 0


async def test_unrelated_concurrent_settings_save_does_not_restore_old_bridge_policy(db_app, monkeypatch):
    from mcp_manager import operations_api

    captured, release = asyncio.Event(), asyncio.Event()
    original = operations_api.settings
    async def paused_settings(request, user):
        result = await original(request, user)
        if request.headers.get("X-Test-Pause") == "1" and not captured.is_set():
            captured.set()
            await release.wait()
        return result
    monkeypatch.setattr(operations_api, "settings", paused_settings)
    pending = asyncio.create_task(db_app.web.patch(
        "/api/v1/settings", json={"title": "concurrent"}, headers={"X-Test-Pause": "1"}))
    try:
        await asyncio.wait_for(captured.wait(), 5)
        response = await db_app.web.patch("/api/v1/settings", json={
            "bridge_heartbeat_seconds": 60, "bridge_missed_heartbeats": 0})
        assert response.status_code == 200, response.text
    finally:
        release.set()
    assert (await pending).status_code == 200
    current = (await db_app.web.get("/api/v1/settings")).json()
    assert current["bridge_heartbeat_seconds"] == 60 and current["bridge_missed_heartbeats"] == 0
