"""Independent lease deadlines and safe automatic runtime reclamation."""
import asyncio
from types import SimpleNamespace

import pytest
from test_runtime import Fake

from mcp_manager.gateway import Gateway
from mcp_manager.runtime import Runtime, ServerSpec


async def test_default_deadline_is_dynamic_stable_and_zero_disables_expiry(monkeypatch):
    ticks, wall = [10.], [1000.]
    monkeypatch.setattr("mcp_manager.runtime.time.time", lambda: wall[0])
    runtime = Runtime(None, clock=lambda: ticks[0])
    assert runtime.connection_ttl_seconds == 43200
    lease = runtime.create_lease("alice", "token", kind="retention")
    first = runtime.lease_status()[0]
    assert first["touched_at"] == 1000 and first["expires_at"] == 44200
    ticks[0], wall[0] = 15., 1006.
    assert runtime.lease_status()[0]["touched_at"] == 1000
    runtime.connection_ttl_seconds = 20
    assert runtime.lease_status()[0]["expires_at"] == 1020
    runtime.heartbeat(lease.id, "alice", "token")
    assert runtime.lease_status()[0]["expires_at"] == 1025
    runtime.connection_ttl_seconds = 0
    ticks[0] += 1_000_000
    await runtime.reap()
    assert lease.id in runtime.leases
    assert runtime.lease_status()[0]["expires_at"] is None
    assert not runtime.lease_status()[0]["expired"]


async def test_connection_timeout_releases_lease_without_waiting_for_instance_idle():
    ticks = [0.]
    fake = Fake()
    runtime = Runtime(fake.connect, clock=lambda: ticks[0], idle_seconds=86400)
    runtime.connection_ttl_seconds = 10
    lease = runtime.create_lease("alice", "token", kind="retention")
    try:
        await runtime.call(ServerSpec("s", "stdio", {}), lease.id, "echo", {})
        ticks[0] = 11
        await runtime.reap()
        assert lease.id not in runtime.leases and not runtime.instances
        assert fake.closes == 1
    finally:
        await runtime.close()


async def test_instance_idle_reclaims_referenced_instance_with_connection_timeout_disabled():
    ticks = [0.]
    fake = Fake()
    runtime = Runtime(fake.connect, clock=lambda: ticks[0], idle_seconds=10)
    runtime.connection_ttl_seconds = 0
    lease = runtime.create_lease("alice", "token", kind="retention")
    spec = ServerSpec("s", "stdio", {})
    try:
        await runtime.call(spec, lease.id, "echo", {})
        ticks[0] = 11
        await runtime.reap()
        assert not runtime.instances and lease.id in runtime.leases
        await runtime.call(spec, lease.id, "echo", {})
        assert fake.starts == 2 and fake.calls == 2
    finally:
        await runtime.close()


async def test_heartbeat_does_not_refresh_instance_idle_clock():
    ticks = [0.]
    fake = Fake()
    runtime = Runtime(fake.connect, clock=lambda: ticks[0], idle_seconds=10)
    runtime.connection_ttl_seconds = 20
    lease = runtime.create_lease("alice", "token", kind="retention")
    try:
        await runtime.call(ServerSpec("s", "stdio", {}), lease.id, "echo", {})
        ticks[0] = 9
        runtime.heartbeat(lease.id, "alice", "token")
        ticks[0] = 11
        await runtime.reap()
        assert lease.id in runtime.leases and not runtime.instances
    finally:
        await runtime.close()


async def test_actual_calls_refresh_connection_heartbeat_and_inflight_is_not_expired():
    ticks = [0.]
    fake = Fake()
    runtime = Runtime(fake.connect, clock=lambda: ticks[0])
    runtime.connection_ttl_seconds = 10
    lease = runtime.create_lease("alice", "token", kind="retention")
    spec = ServerSpec("s", "stdio", {"stop_timeout": .01})
    try:
        ticks[0] = 9
        await runtime.call(spec, lease.id, "echo", {})
        assert lease.touched == 9
        fake.block = True
        fake.entered.clear()
        ticks[0] = 18
        active = asyncio.create_task(runtime.call(spec, lease.id, "echo", {}))
        await fake.entered.wait()
        ticks[0] = 50
        await runtime.reap()
        assert lease.id in runtime.leases and not active.done()
        fake.unblock.set()
        await active
        assert lease.touched == 50
    finally:
        fake.unblock.set()
        await runtime.close()


async def test_idle_stop_rechecks_activity_under_instance_lock(monkeypatch):
    ticks = [0.]
    fake = Fake()
    runtime = Runtime(fake.connect, clock=lambda: ticks[0], idle_seconds=86400)
    runtime.connection_ttl_seconds = 0
    lease = runtime.create_lease("alice", "token")
    spec = ServerSpec("s", "stdio", {"stop_timeout": .01})
    attempted, proceed = asyncio.Event(), asyncio.Event()
    original = runtime._stop_instance
    async def delayed(instance, **kwargs):
        attempted.set()
        await proceed.wait()
        return await original(instance, **kwargs)
    try:
        await runtime.call(spec, lease.id, "echo", {})
        monkeypatch.setattr(runtime, "_stop_instance", delayed)
        ticks[0] = 86400
        reaping = asyncio.create_task(runtime.reap())
        await attempted.wait()
        fake.block = True
        fake.entered.clear()
        active = asyncio.create_task(runtime.call(spec, lease.id, "echo", {}))
        await fake.entered.wait()
        proceed.set()
        await reaping
        assert next(iter(runtime.instances.values())).phase == "ready"
        assert lease.id in next(iter(runtime.instances.values())).refs
        fake.unblock.set()
        await active
        assert fake.calls == 2
    finally:
        proceed.set()
        fake.unblock.set()
        await runtime.close()


async def test_simultaneous_timeout_paths_close_transport_once(monkeypatch):
    ticks = [0.]
    fake = Fake()
    runtime = Runtime(fake.connect, clock=lambda: ticks[0], idle_seconds=10)
    runtime.connection_ttl_seconds = 10
    lease = runtime.create_lease("alice", "token", kind="retention")
    try:
        await runtime.call(ServerSpec("s", "stdio", {}), lease.id, "echo", {})
        instance = next(iter(runtime.instances.values()))
        stops = []
        original = instance.stop.set
        def stopped():
            stops.append(1)
            original()
        monkeypatch.setattr(instance.stop, "set", stopped)
        ticks[0] = 11
        await asyncio.gather(runtime.reap(), runtime.reap(), runtime.stop_instance(instance))
        assert fake.closes == 1 and len(stops) == 1
        assert lease.id not in runtime.leases and not runtime.instances
    finally:
        await runtime.close()


@pytest.mark.parametrize("mode", ["lazy", "eager"])
async def test_last_business_release_stops_but_maintenance_keeps_eager_warm(mode):
    fake = Fake()
    runtime = Runtime(fake.connect)
    spec = ServerSpec("s", "stdio", {}, mode=mode)
    try:
        warm = runtime.create_lease("system", "maintenance", kind="maintenance")
        await runtime.discover(spec, warm.id)
        await runtime.release(warm.id, keep_alive=True)
        assert runtime.instances
        lease = runtime.create_lease("alice", "token", kind="retention")
        await runtime.call(spec, lease.id, "echo", {})
        await runtime.release(lease.id)
        assert bool(runtime.instances) is (mode == "eager")
    finally:
        await runtime.close()


async def test_setting_persists_and_changes_existing_lease_deadline(db_app):
    h = db_app
    response = await h.web.get("/api/v1/settings")
    assert response.json()["connection_ttl_seconds"] == 43200
    runtime = h.app.state.runtime
    lease = runtime.create_lease(h.actor["id"], "", kind="retention")
    first = (await h.web.get("/api/v1/leases/" + lease.id)).json()
    for value in (25, 0):
        response = await h.web.patch("/api/v1/settings", json={"connection_ttl_seconds": value})
        assert response.status_code == 200, response.text
        assert (await h.web.get("/api/v1/settings")).json()["connection_ttl_seconds"] == value
        data = (await h.web.get("/api/v1/leases/" + lease.id)).json()
        assert data["touched_at"] == first["touched_at"]
        assert data["ttl"] == (value or None)
    for value in (-1, True, 1.5, "10", 2**31):
        assert (await h.web.patch("/api/v1/settings", json={"connection_ttl_seconds": value})).status_code == 422


async def test_session_timeout_uses_connection_clock_not_instance_idle(monkeypatch):
    from contextlib import asynccontextmanager
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0], idle_seconds=1)
    runtime.connection_ttl_seconds = 10
    class DB:
        @asynccontextmanager
        async def session(self):
            yield self
        async def execute(self, _statement):
            return [(SimpleNamespace(id="token", disabled=False, expires_at=None),
                     SimpleNamespace(id="alice", disabled=False))]
    state = SimpleNamespace(runtime=runtime, db=DB())
    gateway = Gateway(SimpleNamespace(state=state))
    lease = runtime.create_lease("alice", "token", kind="retention")
    gateway.sessions["sid"] = {"owner": ("alice", "token"), "lease": lease.id, "touched": -100000}
    closed = []
    original = gateway.close_session
    async def close(sid):
        closed.append(sid)
        await original(sid)
    monkeypatch.setattr(gateway, "close_session", close)
    ticks[0] = 2
    await gateway.reap()
    assert not closed and lease.id in runtime.leases
    ticks[0] = 11
    await gateway.reap()
    assert closed == ["sid"] and lease.id not in runtime.leases


async def test_queued_business_request_is_not_expired_while_waiting_for_capacity():
    from mcp_manager.runtime import RuntimeCapacity
    ticks = [0.]
    fake = Fake()
    fake.block = True
    runtime = Runtime(fake.connect, clock=lambda: ticks[0], capacity=RuntimeCapacity(global_calls=1))
    runtime.connection_ttl_seconds = 10
    one = runtime.create_lease("alice", "one", kind="retention")
    two = runtime.create_lease("bob", "two", kind="retention")
    spec = ServerSpec("s", "stdio", {"stop_timeout": .01})
    active = asyncio.create_task(runtime.call(spec, one.id, "echo", {}))
    await fake.entered.wait()
    ticks[0] = 9
    queued = asyncio.create_task(runtime.call(spec, two.id, "echo", {}))
    try:
        for _ in range(50):
            if runtime.capacity_status()["queued_business_calls"]:
                break
            await asyncio.sleep(0)
        assert runtime.capacity_status()["queued_business_calls"] == 1
        ticks[0] = 50
        await runtime.reap()
        assert two.id in runtime.leases and one.id in runtime.leases
        fake.unblock.set()
        await asyncio.gather(active, queued)
        assert fake.calls == 2
        assert two.touched == 50
    finally:
        fake.unblock.set()
        await asyncio.gather(active, queued, return_exceptions=True)
        await runtime.close()


async def test_capacity_does_not_automatically_evict_eager_instances():
    from mcp_manager.runtime import GatewayError, RuntimeCapacity
    fake = Fake()
    runtime = Runtime(fake.connect, capacity=RuntimeCapacity(global_instances=1, user_instances=1))
    lease = runtime.create_lease("alice", "token")
    try:
        await runtime.call(ServerSpec("resident", "stdio", {}, mode="eager"), lease.id, "echo", {})
        with pytest.raises(GatewayError) as caught:
            await runtime.call(ServerSpec("other", "stdio", {}), lease.id, "echo", {})
        assert caught.value.code == "busy"
        assert fake.starts == 1 and fake.closes == 0
    finally:
        await runtime.close()


@pytest.mark.parametrize("kind", ["agent", "bridge", "retention"])
async def test_all_business_lease_kinds_use_current_global_timeout(kind):
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0])
    runtime.connection_ttl_seconds = 20
    lease = runtime.create_lease("alice", "token", ttl=1, kind=kind)
    ticks[0] = 2
    assert runtime.check_lease(lease.id) is lease
    assert runtime.lease_status()[0]["ttl"] == 20
    runtime.connection_ttl_seconds = 0
    ticks[0] = 100
    await runtime.reap()
    assert lease.id in runtime.leases


async def test_bridge_health_is_three_missed_intervals_and_independent_of_zero_ttl():
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0], connection_ttl_seconds=0)
    assert runtime.bridge_heartbeat_seconds == 90
    assert runtime.bridge_missed_heartbeats == 3
    lease = runtime.create_lease("alice", "token", kind="bridge")
    ticks[0] = 269
    assert runtime.check_lease(lease.id) is lease
    assert runtime.lease_status()[0]["bridge_missed_heartbeats"] == 2
    runtime.heartbeat(lease.id, "alice", "token")
    assert runtime.lease_status()[0]["bridge_missed_heartbeats"] == 0
    ticks[0] = 538
    await runtime.reap()
    assert lease.id in runtime.leases
    ticks[0] = 539
    await runtime.reap()
    assert lease.id not in runtime.leases


async def test_bridge_business_calls_do_not_replace_health_heartbeat():
    ticks = [0.]
    fake = Fake()
    runtime = Runtime(fake.connect, clock=lambda: ticks[0], connection_ttl_seconds=0)
    lease = runtime.create_lease("alice", "token", kind="bridge")
    try:
        ticks[0] = 269
        await runtime.call(ServerSpec("s", "stdio", {}, mode="eager"), lease.id, "echo", {})
        ticks[0] = 270
        await runtime.reap()
        assert lease.id not in runtime.leases
        assert runtime.instances and fake.closes == 0
    finally:
        await runtime.close()


async def test_bridge_heartbeat_settings_are_validated_persisted_and_advertised(db_app):
    h = db_app
    original = (await h.web.get("/api/v1/settings")).json()
    assert original["bridge_heartbeat_seconds"] == 90
    assert original["bridge_missed_heartbeats"] == 3
    values = {"bridge_heartbeat_seconds": 12, "bridge_missed_heartbeats": 4}
    response = await h.web.patch("/api/v1/settings", json=values)
    assert response.status_code == 200, response.text
    for key, value in values.items():
        assert (await h.web.get("/api/v1/settings")).json()[key] == value
        assert getattr(h.app.state.runtime, key) == value
        for bad in (-1, True, 1.5, "10", 2**31) + ((0,) if key == "bridge_missed_heartbeats" else ()):
            assert (await h.web.patch("/api/v1/settings", json={key: bad})).status_code == 422


async def test_shortening_bridge_policy_waits_for_client_heartbeat_negotiation():
    ticks = [0.]
    runtime = Runtime(None, clock=lambda: ticks[0], connection_ttl_seconds=0)
    lease = runtime.create_lease("alice", "token", kind="bridge")
    runtime.bridge_heartbeat_seconds = 10
    runtime.bridge_missed_heartbeats = 2
    ticks[0] = 89
    assert runtime.check_lease(lease.id) is lease
    assert runtime.lease_status()[0]["bridge_heartbeat_seconds"] == 90
    ticks[0] = 90
    runtime.heartbeat(lease.id, "alice", "token")
    assert runtime.lease_status()[0]["bridge_heartbeat_seconds"] == 10
    ticks[0] = 109
    assert runtime.check_lease(lease.id) is lease
    ticks[0] = 110
    await runtime.reap()
    assert lease.id not in runtime.leases


async def test_missing_bridge_heartbeat_does_not_interrupt_dispatched_business():
    ticks = [0.]
    fake = Fake()
    fake.block = True
    runtime = Runtime(fake.connect, clock=lambda: ticks[0], connection_ttl_seconds=0)
    lease = runtime.create_lease("alice", "token", kind="bridge")
    active = asyncio.create_task(runtime.call(ServerSpec("s", "stdio", {}), lease.id, "echo", {}))
    try:
        await fake.entered.wait()
        ticks[0] = 1000
        await runtime.reap()
        assert not active.done() and lease.id in runtime.leases
        fake.unblock.set()
        await active
        await runtime.reap()
        assert lease.id not in runtime.leases and fake.calls == 1 and fake.closes == 1
    finally:
        fake.unblock.set()
        await asyncio.gather(active, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("by_token", [False, True])
async def test_revocation_closes_detached_eager_session_without_touching_shared_or_other_tokens(by_token):
    fake = Fake()
    runtime = Runtime(fake.connect)
    try:
        for name, token, isolation in [("owned", "revoked", "session"), ("other", "other", "session"),
                                        ("shared", "revoked", "service")]:
            lease = runtime.create_lease("alice", token)
            await runtime.call(ServerSpec(name, "stdio", {}, mode="eager", isolation=isolation),
                               lease.id, "echo", {})
            await runtime.release(lease.id)
        await runtime.revoke(**({"token_id": "revoked"} if by_token else {"user_id": "alice"}))
        expected = {"other", "shared"} if by_token else {"shared"}
        assert {i.spec.id for i in runtime.instances.values()} == expected
    finally:
        await runtime.close()


@pytest.mark.parametrize("change", ["token_expired", "user_disabled", "maintenance_user_disabled", "scope", "valid"])
async def test_maintenance_revalidates_detached_eager_session_identity(db_app, change):
    import importlib
    from datetime import timedelta

    from mcp_manager.database import ApiToken, McpServer, User, now
    h = db_app
    runtime = h.app.state.runtime
    runtime.connector = Fake().connect
    async with h.db.locked() as session:
        user = await session.get(User, h.actor["id"])
        user.role = "user"
        user.scope_mode = "all"
        token = ApiToken(user_id=user.id, name="session owner", prefix="owner", token_hash="owner-test-hash", scope_mode="all")
        server = McpServer(name="Owned", slug="owned", transport="stdio", config={}, mode="eager")
        session.add_all([token, server])
        await session.flush()
        token_id, server_id = token.id, server.id
    lease = runtime.create_lease(h.actor["id"], "maintenance" if change == "maintenance_user_disabled" else token_id)
    await runtime.call(ServerSpec(server_id, "stdio", {}, mode="eager", isolation="session"), lease.id, "echo", {})
    await runtime.release(lease.id)
    assert runtime.instances
    async with h.db.locked() as session:
        if change == "token_expired":
            (await session.get(ApiToken, token_id)).expires_at = now() - timedelta(seconds=1)
        elif change in {"user_disabled", "maintenance_user_disabled"}:
            (await session.get(User, h.actor["id"])).disabled = True
        elif change == "scope":
            (await session.get(ApiToken, token_id)).scope_mode = "selected"
    await importlib.import_module("mcp_manager.app")._clean_instances(h.app)
    assert bool(runtime.instances) is (change == "valid")


async def test_maintenance_preserves_internal_and_allowed_anonymous_session_instances(db_app):
    import importlib

    from mcp_manager.database import McpServer, set_setting
    h = db_app
    runtime = h.app.state.runtime
    runtime.connector = Fake().connect
    async with h.db.locked() as session:
        server = McpServer(name="Anonymous", slug="anonymous", transport="stdio", config={}, mode="eager")
        session.add(server)
        await session.flush()
        server_id = server.id
    await set_setting(h.db, "token_auth_enabled", False)
    await set_setting(h.db, "anonymous_scope_mode", "all")
    for user, token, name in [("anonymous", "opaque-client", server_id), ("system", "maintenance", server_id)]:
        lease = runtime.create_lease(user, token, kind="maintenance" if user == "system" else "retention")
        await runtime.call(ServerSpec(name, "stdio", {}, mode="eager", isolation="session"), lease.id, "echo", {})
        await runtime.release(lease.id)
    clean = importlib.import_module("mcp_manager.app")._clean_instances
    await clean(h.app)
    assert len(runtime.instances) == 2
    await set_setting(h.db, "token_auth_enabled", True)
    await clean(h.app)
    assert {i.admitted_token for i in runtime.instances.values()} == {"maintenance"}
