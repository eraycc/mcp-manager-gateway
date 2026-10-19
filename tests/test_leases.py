"""Connection management: scoped snapshots, lifecycle and safe disconnection."""
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from test_runtime import Fake

from mcp_manager.database import ApiToken, User
from mcp_manager.gateway import Gateway
from mcp_manager.runtime import Runtime, ServerSpec


async def test_snapshot_has_multiple_instances_and_lease_activity():
    fake = Fake()
    runtime = Runtime(fake.connect, clock=lambda: 100., connection_ttl_seconds=90)
    a = runtime.create_lease("alice", "token", ttl=90, kind="bridge")
    b = runtime.create_lease("bob", "other")
    try:
        await runtime.call(ServerSpec("s1", "stdio", {}), a.id, "echo", {})
        await runtime.call(ServerSpec("s2", "stdio", {}), a.id, "echo", {})
        await runtime.call(ServerSpec("s1", "stdio", {}), b.id, "echo", {})
        fake.block = True
        fake.entered.clear()
        task = asyncio.create_task(runtime.call(ServerSpec("s1", "stdio", {}), a.id, "echo", {}))
        await fake.entered.wait()
        rows = {r["id"]: r for r in runtime.lease_status()}
        assert len(rows[a.id]["instances"]) == 2
        assert rows[a.id]["in_flight"] == 1
        assert rows[b.id]["in_flight"] == 0
        assert rows[a.id]["expires_in"] == 90
        assert abs(rows[a.id]["created_at"] - datetime.now(UTC).timestamp()) < 5
        assert len(runtime.lease_status(user_id="alice")) == 1
        fake.unblock.set()
        await task
        assert runtime.lease_status()[0]["in_flight"] == 0
    finally:
        fake.unblock.set()
        await runtime.close()


@pytest.mark.parametrize("mode,remaining,expected", [
    ("lazy", False, "stopped"), ("lazy", True, "shared"), ("eager", False, "kept_alive"),
])
async def test_disconnect_reports_actual_instance_lifecycle(mode, remaining, expected):
    fake = Fake()
    runtime = Runtime(fake.connect)
    gateway = Gateway(SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    a = runtime.create_lease("alice", "token", kind="retention")
    b = runtime.create_lease("bob", "other")
    spec = ServerSpec("s1", "stdio", {}, mode=mode)
    try:
        await runtime.call(spec, a.id, "echo", {})
        if remaining:
            await runtime.call(spec, b.id, "echo", {})
        gateway.retention[("alice", "token")] = a.id
        result = await gateway.disconnect_lease(a.id)
        assert result["ok"] and result["released"]
        assert result["instances"][0]["outcome"] == expected
        assert ("alice", "token") not in gateway.retention
        assert (await gateway.disconnect_lease(a.id))["already_released"]
    finally:
        await runtime.close()


async def test_disconnect_closes_both_bindings_and_reports_partial_failure(monkeypatch):
    runtime = Runtime(None)
    gateway = Gateway(SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    bridge = runtime.create_lease("alice", "token", kind="bridge")
    session = runtime.create_lease("alice", "token", kind="retention")
    gateway.sessions["session-secret"] = {
        "owner": ("alice", "token"), "lease": session.id, "explicit_lease": bridge.id,
    }
    async def fail(_sid):
        raise RuntimeError("private transport URL with secret")
    monkeypatch.setattr(gateway, "close_session", fail)
    result = await gateway.disconnect_lease(bridge.id)
    assert not result["ok"] and result["released"]
    assert not result["session_cleanup_ok"]
    assert bridge.id not in runtime.leases
    assert "secret" not in str(result)


async def seed(harness):
    async with harness.db.locked() as s:
        a = User(username="alice", password_hash="unused", role="user", scope_mode="all")
        b = User(username="bob", password_hash="unused", role="user", scope_mode="all")
        s.add_all([a, b])
        await s.flush()
        token = ApiToken(user_id=a.id, name="desktop", prefix="test", token_hash="fake-test-hash")
        s.add(token)
        await s.flush()
        ids = a.id, b.id, token.id
    runtime = harness.app.state.runtime
    one = runtime.create_lease(ids[0], ids[2], ttl=90, kind="bridge")
    two = runtime.create_lease(ids[1], "bob-token", kind="retention")
    anon = runtime.create_lease("anonymous", "session:DO-NOT-EXPOSE", kind="retention")
    maintenance = runtime.create_lease(ids[0], "maintenance", kind="maintenance")
    return ids, one, two, anon, maintenance


@asynccontextmanager
async def login_as(harness, user_id):
    # Use a real session, with a fixture-only password hash update.
    from mcp_manager.identity import hasher
    async with harness.db.locked() as s:
        user = await s.get(User, user_id)
        user.password_hash = hasher.hash("password12345")
        name = user.username
    client = httpx.AsyncClient(transport=httpx.ASGITransport(harness.app), base_url="http://test")
    response = await client.post("/api/v1/auth/login", json={"username": name, "password": "password12345"})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]
    try:
        yield client
    finally:
        await client.aclose()


async def test_list_detail_and_disconnect_enforce_scope_and_hide_anonymous_identity(db_app):
    h = db_app
    ids, one, two, anon, maintenance = await seed(h)
    response = await h.web.get("/api/v1/leases")
    assert response.status_code == 200, response.text
    data = response.json()
    assert {x["id"] for x in data["items"]} == {one.id, two.id, anon.id}
    assert "DO-NOT-EXPOSE" not in response.text
    assert "session:" not in response.text
    assert "DO-NOT-EXPOSE" not in (await h.web.get("/api/v1/leases/" + anon.id)).text
    assert (await h.web.post("/api/v1/leases/" + maintenance.id + "/disconnect")).status_code == 403
    async with login_as(h, ids[0]) as user:
        own = (await user.get("/api/v1/leases?user_id=" + ids[1])).json()
        assert {x["id"] for x in own["items"]} == {one.id}
        assert [x["user_id"] for x in own["user_refs"]] == [ids[0]]
        assert (await user.get("/api/v1/leases/" + two.id)).status_code == 404
        assert (await user.post("/api/v1/leases/" + two.id + "/disconnect")).status_code == 403
        user.headers.pop("X-CSRF-Token")
        assert (await user.post("/api/v1/leases/" + one.id + "/disconnect")).status_code == 403
        user.headers["X-CSRF-Token"] = user.cookies["mcp_csrf"]
        r = await user.post("/api/v1/leases/batch", json={"lease_ids": [one.id, two.id, "missing", one.id]})
        assert r.status_code == 200, r.text
        assert r.json()["summary"] == {"total": 3, "ok": 2, "failed": 1}
    assert one.id not in h.app.state.runtime.leases and two.id in h.app.state.runtime.leases


async def test_filters_paging_expiry_and_details_do_not_leak_shared_users(db_app):
    from mcp_manager.runtime import Instance
    h = db_app
    ids, one, two, _anon, maintenance = await seed(h)
    runtime = h.app.state.runtime
    spec = ServerSpec("mcp-a", "stdio", {"env": {"SECRET": "never-show"}})
    instance = Instance(("mcp-a",), spec, refs={one.id, two.id, maintenance.id}, phase="ready")
    second = Instance(("mcp-a", 2), spec, refs={one.id}, phase="ready")
    runtime.instances[instance.key] = instance
    runtime.instances[second.key] = second
    try:
        data = (await h.web.get("/api/v1/leases?q=desktop&page_size=1")).json()
        assert data["total"] == 1 and data["items"][0]["token_name"] == "desktop"
        assert data["mcp_refs"][0]["ref_count"] == 2
        assert (await h.web.get("/api/v1/leases?mcp_id=mcp-a")).json()["total"] == 2
        one.touched -= h.app.state.runtime.connection_ttl_seconds + 10
        expired = (await h.web.get("/api/v1/leases?status=expired")).json()
        assert expired["items"][0]["can_disconnect"] and expired["total"] == 1
        for query in ["page_size=201", "kind=invalid", "sort=invalid", "touched_from=garbage",
                      "touched_from=2026-10-20T00:00:00Z&touched_to=2026-10-19T00:00:00Z"]:
            assert (await h.web.get("/api/v1/leases?" + query)).status_code == 422
        async with login_as(h, ids[0]) as user:
            result = await user.get("/api/v1/leases/" + one.id)
            assert result.status_code == 200
            assert "bob" not in result.text and two.id not in result.text and "never-show" not in result.text
            assert len(result.json()["instances"]) == 2
        assert (await h.web.post("/api/v1/leases/batch", json={"lease_ids": []})).status_code == 422
    finally:
        runtime.instances.clear()


async def test_disconnect_drains_inflight_without_claiming_cancellation():
    fake = Fake()
    fake.block = True
    runtime = Runtime(fake.connect)
    gateway = Gateway(SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    lease = runtime.create_lease("alice", "token")
    task = asyncio.create_task(runtime.call(
        ServerSpec("slow", "stdio", {"stop_timeout": .01}), lease.id, "echo", {}))
    await fake.entered.wait()
    try:
        result = await gateway.disconnect_lease(lease.id)
        assert result["instances"][0]["outcome"] == "draining"
        assert not task.done()
        fake.unblock.set()
        await task
        assert not runtime.instances
    finally:
        fake.unblock.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def test_batch_revalidates_permissions_between_items(db_app, monkeypatch):
    h = db_app
    _ids, one, two, _anon, _maintenance = await seed(h)
    gateway = h.app.state.gateway
    original = gateway.disconnect_lease
    async def demote(lease_id):
        result = await original(lease_id)
        async with h.db.locked() as session:
            admin = await session.get(User, h.actor["id"])
            admin.role = "user"
        return result
    monkeypatch.setattr(gateway, "disconnect_lease", demote)
    response = await h.web.post("/api/v1/leases/batch", json={"lease_ids": [one.id, two.id]})
    assert response.status_code == 200, response.text
    assert response.json()["summary"] == {"total": 2, "ok": 1, "failed": 1}
    assert two.id in h.app.state.runtime.leases


async def test_quick_calls_keep_history_per_lease_and_ignore_heartbeats_and_discovery(monkeypatch):
    ticks = [10.]
    monkeypatch.setattr("mcp_manager.runtime.time.time", lambda: 1000.)
    fake = Fake()
    runtime = Runtime(fake.connect, clock=lambda: ticks[0])
    a, b = runtime.create_lease("alice", "one"), runtime.create_lease("alice", "two")
    spec = ServerSpec("s", "stdio", {})
    try:
        await runtime.discover(spec, a.id)
        runtime.heartbeat(a.id, "alice", "one")
        first = runtime.lease_status()[0]
        assert first["call_count"] == 0 and first["last_call_at"] is None
        ticks[0] = 12
        await runtime.call(spec, a.id, "echo", {})
        await runtime.call(spec, b.id, "echo", {})
        ticks[0] = 13
        await runtime.call(spec, a.id, "echo", {})
        ticks[0] = 20
        rows = {r["id"]: r for r in runtime.lease_status()}
        assert rows[a.id]["in_flight"] == 0 and rows[a.id]["status"] == "idle"
        assert rows[a.id]["call_count"] == 2 and rows[b.id]["call_count"] == 1
        assert rows[a.id]["last_call_at"] == 1003
        ticks[0] = 30
        runtime.heartbeat(a.id, "alice", "one")
        assert runtime.lease_status()[0]["last_call_at"] == 1003
    finally:
        await runtime.close()


async def test_waiting_call_is_active_but_cancelled_before_dispatch_does_not_count():
    fake = Fake()
    fake.block = True
    runtime = Runtime(fake.connect)
    a, b = runtime.create_lease("alice", "one"), runtime.create_lease("alice", "two")
    spec = ServerSpec("s", "stdio", {})
    active = asyncio.create_task(runtime.call(spec, a.id, "echo", {}))
    queued = None
    try:
        await fake.entered.wait()
        queued = asyncio.create_task(runtime.call(spec, b.id, "echo", {}))
        for _ in range(100):
            if b.pending:
                break
            await asyncio.sleep(0)
        rows = {r["id"]: r for r in runtime.lease_status()}
        assert rows[b.id]["status"] == "active"
        assert rows[b.id]["queued_calls"] == 1 and rows[b.id]["in_flight"] == 0
        assert rows[b.id]["call_count"] == 0 and rows[b.id]["last_call_at"] is None
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        after = next(r for r in runtime.lease_status() if r["id"] == b.id)
        assert after["call_count"] == 0 and after["queued_calls"] == 0 and after["status"] == "idle"
    finally:
        fake.unblock.set()
        await asyncio.gather(*(t for t in (active, queued) if t), return_exceptions=True)
        await runtime.close()


async def test_failed_dispatched_call_counts_once_without_replay():
    from mcp_manager.runtime import GatewayError

    fake = Fake()
    fake.fail = True
    runtime = Runtime(fake.connect)
    lease = runtime.create_lease("alice", "one")
    try:
        with pytest.raises(GatewayError) as caught:
            await runtime.call(ServerSpec("s", "stdio", {}), lease.id, "echo", {})
        assert caught.value.code == "outcome_unknown"
        assert runtime.lease_status()[0]["call_count"] == fake.calls == 1
        assert runtime.lease_status()[0]["last_call_at"] is not None
    finally:
        await runtime.close()
