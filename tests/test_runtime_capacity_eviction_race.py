"""Two admissions can select one idle victim; the loser must recheck other idle slots."""
import asyncio
from contextlib import asynccontextmanager

from mcp_manager.runtime import Runtime, RuntimeCapacity, ServerSpec


async def test_concurrent_new_mcps_recheck_room_after_a_shared_idle_eviction(monkeypatch):
    active, peak, calls = 0, 0, 0
    class Endpoint:
        async def call(self, _name, arguments):
            nonlocal calls
            calls += 1
            await asyncio.sleep(.001)
            return {"value": arguments["value"]}
    @asynccontextmanager
    async def connect(_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            yield Endpoint()
        finally:
            active -= 1
    runtime = Runtime(connect, capacity=RuntimeCapacity(
        global_calls=2, user_calls=2, mcp_calls=2, queued_calls=128,
        global_instances=2, user_instances=2))
    lease = runtime.create_lease("owner", "token")
    async def invoke(identity):
        return await runtime.call(ServerSpec(str(identity), "stdio", {}, "lazy"),
                                  lease.id, "echo", {"value": identity})
    try:
        await invoke(0)
        await invoke(1)
        original = runtime._stop_instance
        shared, selected = asyncio.Event(), []
        async def stop(instance, **kwargs):
            if kwargs.get("reason") == "capacity_eviction" and instance.spec.id == "0":
                selected.append(instance)
                if len(selected) == 2:
                    shared.set()
                await asyncio.wait_for(shared.wait(), 2)
            return await original(instance, **kwargs)
        monkeypatch.setattr(runtime, "_stop_instance", stop)
        results = await asyncio.gather(invoke(2), invoke(3))
        assert [result["value"] for result in results] == [2, 3]
        assert calls == 4 and peak <= 2
    finally:
        await runtime.close()
    assert active == 0
