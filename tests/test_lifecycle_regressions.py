import asyncio

import pytest

from mcp_manager.logs import LogStore
from mcp_manager.runtime import GatewayError, Runtime
from test_runtime import Fake, spec


async def test_stop_rejects_queued_dispatch():
    fake = Fake()
    fake.block = True
    runtime = Runtime(fake.connect)
    lease = runtime.create_lease("u", "t")
    first = asyncio.create_task(runtime.call(spec(), lease.id, "first", {}))
    await fake.entered.wait()
    queued = asyncio.create_task(runtime.call(spec(), lease.id, "second", {}))
    await asyncio.sleep(.02)
    stopping = asyncio.create_task(runtime.stop_server("s1", hold=True))
    await asyncio.sleep(.02)
    fake.unblock.set()
    await first
    with pytest.raises(GatewayError, match="stopped"):
        await queued
    await stopping
    assert fake.calls == 1
    assert fake.closes == 1
    await runtime.close()


async def test_torn_tail_does_not_consume_next_event(tmp_path):
    store = LogStore(tmp_path)
    await store.append({"id": "before", "timestamp": "2026-09-11T00:00:00+00:00", "status": "success"})
    with (tmp_path / "logs/2026-09-11.jsonl").open("ab") as f:
        f.write(b'{"id":"broken')
    await store.append({"id": "after", "timestamp": "2026-09-11T00:00:01+00:00", "status": "success"})
    await store.rebuild()
    assert (await store.query())["total"] == 2
    await store.close()


async def test_deleted_running_call_cannot_resurrect(tmp_path):
    store = LogStore(tmp_path)
    await store.append({"id": "call", "status": "running", "user_id": "u"})
    await store.delete(["call"])
    await store.append({"id": "call", "status": "success", "user_id": "u"})
    assert (await store.query())["total"] == 0
    await store.close()
    reopened = LogStore(tmp_path)
    assert (await reopened.query())["total"] == 0
    await reopened.close()
