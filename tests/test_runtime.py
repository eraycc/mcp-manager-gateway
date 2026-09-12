import asyncio
from contextlib import asynccontextmanager
import pytest
from mcp_manager.runtime import Runtime, ServerSpec, GatewayError


class Fake:
    def __init__(self):
        self.starts = 0
        self.closes = 0
        self.calls = 0
        self.entered = asyncio.Event()
        self.unblock = asyncio.Event()
        self.block = False
        self.fail = False

    @asynccontextmanager
    async def connect(self, spec):
        self.starts += 1
        await asyncio.sleep(.005)
        try:
            yield self
        finally:
            self.closes += 1

    async def call(self, name, arguments):
        self.calls += 1
        self.entered.set()
        if self.block:
            await self.unblock.wait()
        if self.fail:
            raise ConnectionError("lost after send")
        return {"content": [{"type": "text", "text": arguments.get("value", "ok")}]}

    async def discover(self):
        return {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}], "resources": [], "prompts": [], "templates": []}


def spec(**kwargs):
    return ServerSpec(id="s1", transport="stdio", config={}, **kwargs)


@pytest.mark.asyncio
async def test_concurrent_first_calls_share_one_instance():
    fake = Fake()
    runtime = Runtime(fake.connect)
    lease = runtime.create_lease("u1", "t1")
    results = await asyncio.gather(*[runtime.call(spec(), lease.id, "echo", {}) for _ in range(100)])
    assert len(results) == 100
    assert fake.starts == 1
    await runtime.close()
    assert fake.closes == 1


@pytest.mark.asyncio
async def test_releasing_one_agent_keeps_other_and_last_closes():
    fake = Fake()
    runtime = Runtime(fake.connect)
    a = runtime.create_lease("u1", "t1")
    b = runtime.create_lease("u1", "t1")
    await runtime.call(spec(), a.id, "echo", {})
    await runtime.call(spec(), b.id, "echo", {})
    await runtime.release(a.id)
    assert fake.closes == 0
    await runtime.release(b.id)
    assert fake.closes == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_idle_reclaims_despite_live_lease_then_restarts():
    now = [0.]
    fake = Fake()
    runtime = Runtime(fake.connect, clock=lambda: now[0], idle_seconds=86400)
    lease = runtime.create_lease("u1", "t1")
    await runtime.call(spec(), lease.id, "echo", {})
    now[0] = 86401
    await runtime.reap()
    assert fake.closes == 1
    await runtime.call(spec(), lease.id, "echo", {})
    assert fake.starts == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_inflight_call_is_not_stopped_by_idle_reaper():
    now = [0.]
    fake = Fake()
    fake.block = True
    runtime = Runtime(fake.connect, clock=lambda: now[0], idle_seconds=10)
    lease = runtime.create_lease("u1", "t1")
    task = asyncio.create_task(runtime.call(spec(), lease.id, "echo", {}))
    await fake.entered.wait()
    now[0] = 100
    await runtime.reap()
    assert fake.closes == 0
    fake.unblock.set()
    await task
    await runtime.close()


@pytest.mark.asyncio
async def test_downstream_call_is_not_replayed_after_connection_loss():
    fake = Fake()
    fake.fail = True
    runtime = Runtime(fake.connect)
    lease = runtime.create_lease("u1", "t1")
    with pytest.raises(GatewayError) as exc:
        await runtime.call(spec(), lease.id, "write", {})
    assert exc.value.code == "outcome_unknown"
    assert fake.calls == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_user_isolation_and_disabled():
    fake = Fake()
    runtime = Runtime(fake.connect)
    a = runtime.create_lease("u1", "t")
    b = runtime.create_lease("u2", "t")
    await runtime.call(spec(isolation="user"), a.id, "echo", {})
    await runtime.call(spec(isolation="user"), b.id, "echo", {})
    assert fake.starts == 2
    with pytest.raises(GatewayError):
        await runtime.call(spec(mode="disabled"), a.id, "echo", {})
    await runtime.close()


@pytest.mark.asyncio
async def test_expired_bridge_lease_releases_instances():
    now = [0.]
    fake = Fake()
    runtime = Runtime(fake.connect, clock=lambda: now[0])
    lease = runtime.create_lease("u", "t", ttl=90)
    await runtime.call(spec(), lease.id, "echo", {})
    now[0] = 91
    await runtime.reap()
    assert fake.closes == 1
    with pytest.raises(GatewayError):
        runtime.heartbeat(lease.id, "u", "t")
    await runtime.close()


@pytest.mark.asyncio
async def test_lease_owner_cannot_be_spoofed():
    runtime = Runtime(Fake().connect)
    lease = runtime.create_lease("u", "t")
    with pytest.raises(GatewayError):
        runtime.heartbeat(lease.id, "intruder", "t")
    await runtime.close()
