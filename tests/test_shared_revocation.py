import asyncio

import pytest
from fastapi import HTTPException

from mcp_manager.runtime import Runtime
from test_runtime import Fake, spec


async def test_revoked_queued_caller_does_not_close_other_users_instance():
    fake = Fake()
    fake.block = True
    runtime = Runtime(fake.connect)
    good = runtime.create_lease("good", "good-token")
    bad = runtime.create_lease("bad", "bad-token")
    checks = 0
    async def authorize():
        nonlocal checks
        checks += 1
        if checks > 1:
            raise HTTPException(401, "Token revoked")
    first = asyncio.create_task(runtime.call(spec(), good.id, "first", {}))
    await fake.entered.wait()
    denied = asyncio.create_task(runtime.call(spec(), bad.id, "denied", {}, authorize=authorize))
    await asyncio.sleep(.02)
    second = asyncio.create_task(runtime.call(spec(), good.id, "second", {}))
    await asyncio.sleep(.02)
    fake.unblock.set()
    await first
    with pytest.raises(HTTPException):
        await denied
    await second
    assert fake.calls == 2 and fake.starts == 1 and fake.closes == 0
    assert runtime.status()[0]["phase"] == "ready"
    assert runtime.status()[0]["lease_count"] == 1
    await runtime.close()
