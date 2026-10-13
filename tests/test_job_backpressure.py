"""No Task-per-waiting-item: bounded workers, admission and cancellation."""
import asyncio

import pytest

from mcp_manager.runtime import GatewayError


async def test_waiting_items_do_not_create_unbounded_tasks(db_app):
    jobs = db_app.app.state.jobs
    entered, release = asyncio.Event(), asyncio.Event()
    async def operation(item):
        entered.set()
        await release.wait()
    before = len(asyncio.all_tasks())
    await jobs.submit("fixture", list(range(200)), operation)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert len(asyncio.all_tasks()) - before <= 8
    finally:
        release.set()
        await jobs.close()


async def test_queue_rejects_more_than_1000_items_before_any_dispatch(db_app):
    jobs = db_app.app.state.jobs
    calls = []
    async def operation(item):
        calls.append(item)
    try:
        with pytest.raises(GatewayError) as caught:
            await jobs.submit("fixture", list(range(1001)), operation)
        assert caught.value.code == "busy" and calls == []
    finally:
        await jobs.close()
