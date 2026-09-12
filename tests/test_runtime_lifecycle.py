import asyncio
from contextlib import asynccontextmanager

import pytest

from mcp_manager.runtime import GatewayError, Runtime, ServerSpec


class Connection:
    def __init__(self):
        self.entered = asyncio.Event()
        self.finish = asyncio.Event()
        self.block = False
        self.closed = 0

    @asynccontextmanager
    async def connect(self, spec):
        try:
            yield self
        finally:
            self.closed += 1

    async def call(self, name, arguments):
        self.entered.set()
        if self.block:
            await self.finish.wait()
        return {"ok": True}

    async def discover(self):
        return {"tools": [], "resources": [], "prompts": [], "templates": []}


@pytest.mark.asyncio
async def test_explicit_warm_detaches_without_leases_and_expires_at_idle():
    now = [0.]
    connection = Connection()
    runtime = Runtime(connection.connect, clock=lambda: now[0], idle_seconds=10)
    spec = ServerSpec("s", "stdio", {})
    try:
        for _ in range(10):
            lease = runtime.create_lease("system", "maintenance", kind="maintenance")
            await runtime.discover(spec, lease.id)
            await runtime.release(lease.id, keep_alive=True)
        assert runtime.leases == {}
        assert len(runtime.status()) == 1
        assert runtime.status()[0]["phase"] == "ready"
        now[0] = 11.
        await runtime.reap()
        assert runtime.status() == []
        assert connection.closed == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_closed_session_instances_and_locks_do_not_accumulate():
    runtime = Runtime(Connection().connect)
    try:
        for _ in range(15):
            lease = runtime.create_lease("user", "token", ttl=90)
            await runtime.call(ServerSpec("s", "stdio", {}, isolation="session"), lease.id, "call", {})
            await runtime.release(lease.id)
        await runtime.reap()
        assert runtime.status() == []
        assert runtime.instances == {}
        assert len(runtime._locks) == 0
        assert runtime.leases == {}
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["lazy", "eager"])
async def test_credential_generation_drains_without_interrupting_active_call(mode):
    connection = Connection()
    connection.block = True
    runtime = Runtime(connection.connect)
    lease = runtime.create_lease("u", "t")
    old = ServerSpec("s", "stdio", {"stop_timeout": .01}, mode=mode,
                     credential_scope="old", credential_owner="service")
    new = ServerSpec("s", "stdio", {"stop_timeout": .01}, mode=mode,
                     credential_scope="new", credential_owner="service")
    active = asyncio.create_task(runtime.call(old, lease.id, "call", {}))
    await connection.entered.wait()
    old_instance = next(iter(runtime.instances.values()))
    old_owner = old_instance.owner
    runtime.credential_versions[("s", "service")] = "new"
    # The stop timeout must not cancel a dispatched business operation.
    await runtime._stop_instance(old_instance)
    assert not active.done()
    assert connection.closed == 0
    next_call = asyncio.create_task(runtime.call(new, lease.id, "call", {}))
    connection.finish.set()
    try:
        assert await asyncio.wait_for(active, 1) == {"ok": True}
        assert await asyncio.wait_for(next_call, 1) == {"ok": True}
        assert old_owner.done()
        assert connection.closed == 1
        assert len(runtime.status()) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_busy_configuration_stop_preserves_old_instance_and_hold():
    connection = Connection()
    connection.block = True
    runtime = Runtime(connection.connect)
    lease = runtime.create_lease("u", "t")
    active = asyncio.create_task(runtime.call(ServerSpec("s", "stdio", {}), lease.id, "call", {}))
    await connection.entered.wait()
    try:
        with pytest.raises(GatewayError) as caught:
            await runtime.stop_server("s", hold=True, require_idle=True)
        assert caught.value.code == "busy"
        assert "s" not in runtime.holds
        assert runtime.status()[0]["phase"] == "ready"
        assert runtime.status()[0]["lease_count"] == 1
        assert connection.closed == 0
    finally:
        connection.finish.set()
        await active
        await runtime.close()


@pytest.mark.asyncio
async def test_connection_callback_completes_before_first_business_dispatch():
    connection = Connection()
    runtime = Runtime(connection.connect)
    lease = runtime.create_lease("u", "t")
    cached = []
    async def on_connect(spec, downstream):
        await asyncio.sleep(0)
        assert not connection.entered.is_set()
        cached.append((spec.id, await downstream.discover()))
    runtime.on_connect = on_connect
    try:
        await runtime.call(ServerSpec("s", "stdio", {}), lease.id, "call", {})
        assert cached == [("s", {"tools": [], "resources": [], "prompts": [], "templates": []})]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_connection_callback_failure_prevents_business_dispatch():
    connection = Connection()
    runtime = Runtime(connection.connect)
    lease = runtime.create_lease("u", "t")
    async def on_connect(spec, downstream):
        raise ValueError("discovery failed")
    runtime.on_connect = on_connect
    try:
        with pytest.raises(GatewayError) as caught:
            await runtime.call(ServerSpec("s", "stdio", {}), lease.id, "call", {})
        assert caught.value.code == "startup_failed"
        assert "discovery failed" in str(caught.value)
        assert not connection.entered.is_set()
        assert runtime.status() == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_startup_timeout_closes_owner_when_discovery_never_returns():
    connection = Connection()
    runtime = Runtime(connection.connect)
    lease = runtime.create_lease("u", "t")
    async def on_connect(spec, downstream):
        await asyncio.Event().wait()
    runtime.on_connect = on_connect
    try:
        with pytest.raises(GatewayError) as caught:
            await runtime.call(ServerSpec("s", "stdio", {"startup_timeout": .01}), lease.id, "call", {})
        assert caught.value.code in {"startup_timeout", "startup_failed"}
        await asyncio.sleep(.02)
        assert connection.closed == 1
        assert runtime.status() == []
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_expired_eager_session_closes_its_connection():
    now = [0.]
    connection = Connection()
    runtime = Runtime(connection.connect, clock=lambda: now[0])
    lease = runtime.create_lease("u", "t", ttl=90)
    try:
        await runtime.call(ServerSpec("s", "stdio", {}, mode="eager", isolation="session"),
                           lease.id, "call", {})
        now[0] = 91.
        await runtime.reap()
        assert connection.closed == 1
        assert runtime.instances == {}
        assert runtime.leases == {}
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("isolation", "credential_owner", "bob_visible"), [
    ("service", "", True),
    ("service", "service", True),
    ("user", "alice", False),
    ("user", "", False),
])
async def test_status_visibility_survives_maintenance_release_without_exposing_other_users(
        isolation, credential_owner, bob_visible):
    runtime = Runtime(Connection().connect)
    lease = runtime.create_lease("alice", "maintenance", kind="maintenance")
    spec = ServerSpec("s", "stdio", {}, mode="eager", isolation=isolation,
                      credential_owner=credential_owner)
    try:
        await runtime.discover(spec, lease.id)
        await runtime.release(lease.id)
        assert len(runtime.status(user_id="alice")) == 1
        assert bool(runtime.status(user_id="bob")) is bob_visible
        assert runtime.leases == {}
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("isolation", "credential_owner"), [
    ("user", ""), ("user", "alice"), ("service", "alice"),
])
async def test_user_revoke_closes_unreferenced_owned_eager_instances_only(isolation, credential_owner):
    runtime = Runtime(Connection().connect)
    try:
        for server_id, user_id, scope, owner in [
            ("owned", "alice", isolation, credential_owner),
            ("other", "bob", "user", "bob"),
            ("shared", "alice", "service", "service"),
        ]:
            lease = runtime.create_lease(user_id, "maintenance", kind="maintenance")
            spec = ServerSpec(server_id, "stdio", {}, mode="eager", isolation=scope,
                              credential_scope="token", credential_owner=owner)
            await runtime.discover(spec, lease.id)
            await runtime.release(lease.id)
        assert runtime.leases == {}
        await runtime.revoke(user_id="alice")
        assert {item["server_id"] for item in runtime.status()} == {"other", "shared"}
        if credential_owner:
            assert runtime.credential_versions[("owned", "alice")] is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_user_revoke_drains_eager_call_without_cancelling_business_result():
    connection = Connection()
    connection.block = True
    runtime = Runtime(connection.connect)
    lease = runtime.create_lease("alice", "token")
    spec = ServerSpec("s", "stdio", {"stop_timeout": .01}, mode="eager", isolation="user",
                      credential_scope="old", credential_owner="alice")
    active = asyncio.create_task(runtime.call(spec, lease.id, "call", {}))
    await connection.entered.wait()
    try:
        await runtime.revoke(user_id="alice")
        assert not active.done()
        assert connection.closed == 0
        assert runtime.status()[0]["phase"] == "draining"
        assert runtime.credential_versions[("s", "alice")] is None
        connection.finish.set()
        assert await active == {"ok": True}
        assert runtime.status() == []
    finally:
        connection.finish.set()
        await active
        await runtime.close()


@pytest.mark.asyncio
async def test_token_scoped_revoke_preserves_other_tokens_personal_eager_instance():
    runtime = Runtime(Connection().connect)
    lease = runtime.create_lease("alice", "other-token")
    spec = ServerSpec("s", "stdio", {}, mode="eager", isolation="user",
                      credential_scope="current", credential_owner="alice")
    try:
        await runtime.discover(spec, lease.id)
        await runtime.revoke(user_id="alice", token_id="revoked-token")
        assert runtime.status()[0]["phase"] == "ready"
        assert await runtime.call(spec, lease.id, "call", {}) == {"ok": True}
    finally:
        await runtime.close()
