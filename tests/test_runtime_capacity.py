"""Runtime admission stays bounded and fair across users and MCP services."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from mcp_manager.runtime import GatewayError, Runtime, RuntimeCapacity, ServerSpec


class CapacityFixture:
    def __init__(self):
        self.starts = []
        self.calls = []
        self.entered = {}
        self.release = {}

    @asynccontextmanager
    async def connect(self, spec):
        self.starts.append(spec.id)
        self.entered.setdefault(spec.id, asyncio.Event())
        self.release.setdefault(spec.id, asyncio.Event())
        yield self

    def event(self, server_id):
        return self.entered.setdefault(server_id, asyncio.Event())

    async def call(self, name, arguments):
        server_id = arguments["server"]
        self.calls.append(server_id)
        self.event(server_id).set()
        if arguments.get("block"):
            await self.release.setdefault(server_id, asyncio.Event()).wait()
        if arguments.get("lose"):
            raise ConnectionError("lost after dispatch")
        return {"content": [{"type": "text", "text": server_id}]}


def spec(server_id, *, concurrency=1):
    return ServerSpec(
        id=server_id,
        transport="stdio",
        config={"concurrency": concurrency, "queue_timeout": 1},
        mode="eager",
    )


@pytest.mark.asyncio
async def test_bridge_pending_dispatch_is_bounded():
    from mcp_manager.bridge import BridgeUpstream

    upstream = BridgeUpstream("http://gateway.invalid", "")
    upstream.task = asyncio.get_running_loop().create_future()
    for _ in range(64):
        upstream.queue.put_nowait((asyncio.get_running_loop().create_future(), "call", (), {}))
    with pytest.raises(RuntimeError, match="queue is full"):
        await upstream.request("call_tool", "name", {})
    assert upstream.queue.qsize() == upstream.dispatch_limit == 64
    upstream.task.cancel()


@pytest.mark.asyncio
async def test_saturation_rejects_before_dispatch_and_slow_mcp_does_not_starve():
    fixture = CapacityFixture()
    runtime = Runtime(
        fixture.connect,
        capacity=RuntimeCapacity(
            global_calls=2,
            user_calls=2,
            mcp_calls=1,
            queued_calls=1,
            global_instances=8,
            user_instances=8,
            per_instance_waiters=2,
            queue_timeout=0.5,
        ),
    )
    first = runtime.create_lease("u1", "t1")
    second = runtime.create_lease("u2", "t2")

    slow = asyncio.create_task(
        runtime.call(spec("slow"), first.id, "write", {"server": "slow", "block": True})
    )
    await fixture.event("slow").wait()
    queued = asyncio.create_task(
        runtime.call(spec("slow"), second.id, "write", {"server": "slow"})
    )
    await asyncio.sleep(0)

    fast_task = asyncio.create_task(
        runtime.call(
            spec("fast"),
            second.id,
            "read",
            {"server": "fast", "block": True},
        )
    )
    await asyncio.wait_for(fixture.event("fast").wait(), 0.25)
    assert fixture.calls == ["slow", "fast"]

    blocked_starts = len(fixture.starts)
    with pytest.raises(GatewayError) as busy:
        await runtime.call(spec("third"), first.id, "write", {"server": "third"})
    assert busy.value.code == "busy"
    assert len(fixture.starts) == blocked_starts
    assert runtime.capacity_status()["queued_business_calls"] == 1

    fixture.release["fast"].set()
    fast = await fast_task
    assert fast["content"][0]["text"] == "fast"
    fixture.release["slow"].set()
    await slow
    await queued
    assert fixture.calls.count("slow") == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_queued_call_reauthorizes_and_unknown_result_is_never_replayed():
    fixture = CapacityFixture()
    runtime = Runtime(
        fixture.connect,
        capacity=RuntimeCapacity(
            global_calls=1,
            user_calls=1,
            mcp_calls=1,
            queued_calls=4,
            global_instances=8,
            user_instances=8,
            per_instance_waiters=4,
            queue_timeout=1,
        ),
    )
    first = runtime.create_lease("u1", "t1")
    second = runtime.create_lease("u2", "t2")
    slow = asyncio.create_task(
        runtime.call(spec("slow"), first.id, "write", {"server": "slow", "block": True})
    )
    await fixture.event("slow").wait()

    checks = 0

    async def revoked():
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise GatewayError("permission_revoked", "revoked while queued")

    denied = asyncio.create_task(
        runtime.call(spec("denied"), second.id, "write", {"server": "denied"}, authorize=revoked)
    )
    await asyncio.sleep(0)
    fixture.release["slow"].set()
    await slow
    with pytest.raises(GatewayError) as error:
        await denied
    assert error.value.code == "permission_revoked"
    assert "denied" not in fixture.starts
    assert "denied" not in fixture.calls

    with pytest.raises(GatewayError) as unknown:
        await runtime.call(spec("loss"), first.id, "write", {"server": "loss", "lose": True})
    assert unknown.value.code == "outcome_unknown"
    assert fixture.calls.count("loss") == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_instance_and_waiter_caps_are_hard_limits():
    fixture = CapacityFixture()
    runtime = Runtime(
        fixture.connect,
        capacity=RuntimeCapacity(
            global_calls=4,
            user_calls=4,
            mcp_calls=4,
            queued_calls=8,
            global_instances=1,
            user_instances=1,
            per_instance_waiters=1,
            queue_timeout=0.5,
        ),
    )
    lease = runtime.create_lease("u1", "t1")
    await runtime.call(spec("resident"), lease.id, "read", {"server": "resident"})
    with pytest.raises(GatewayError) as instances:
        await runtime.call(spec("other"), lease.id, "read", {"server": "other"})
    assert instances.value.code == "busy"
    assert fixture.starts == ["resident"]
    await runtime.close()

    fixture = CapacityFixture()
    runtime = Runtime(
        fixture.connect,
        capacity=RuntimeCapacity(
            global_calls=4,
            user_calls=4,
            mcp_calls=4,
            queued_calls=8,
            global_instances=4,
            user_instances=4,
            per_instance_waiters=1,
            queue_timeout=0.5,
        ),
    )
    lease = runtime.create_lease("u1", "t1")
    active = asyncio.create_task(
        runtime.call(
            spec("one", concurrency=1),
            lease.id,
            "write",
            {"server": "one", "block": True},
        )
    )
    await fixture.event("one").wait()
    waiter = asyncio.create_task(
        runtime.call(spec("one", concurrency=1), lease.id, "read", {"server": "one"})
    )
    await asyncio.sleep(0)
    with pytest.raises(GatewayError) as waiters:
        await runtime.call(spec("one", concurrency=1), lease.id, "read", {"server": "one"})
    assert waiters.value.code == "busy"
    fixture.release["one"].set()
    await active
    await waiter
    await runtime.close()
