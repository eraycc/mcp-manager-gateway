"""Repeated cancellation must not interrupt another item's durable cleanup."""
import asyncio

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.job_store import JobStore
from mcp_manager.jobs import Jobs


async def test_batch_cancellation_waits_for_every_active_cleanup(dual_db, db_settings, monkeypatch):
    store = JobStore(dual_db, ArtifactStore(db_settings.data_dir))
    jobs = Jobs(store=store)
    await jobs.start()
    started = [asyncio.Event(), asyncio.Event()]
    cleaning_second, release_cleanup, first_recorded = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []
    append = store.append_result

    async def record(identity, ordinal, result):
        value = await append(identity, ordinal, result)
        if ordinal == 0:
            first_recorded.set()
        return value

    monkeypatch.setattr(store, "append_result", record)

    async def operation(item):
        started[item].set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if item == 0:
                await cleaning_second.wait()
            else:
                cleaning_second.set()
                await release_cleanup.wait()
                cleaned.append(item)
            raise

    try:
        job = await jobs.submit("cancel-cleanup", [0, 1], operation)
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 5)
        cancelled = asyncio.create_task(jobs.cancel(job["id"]))
        await asyncio.wait_for(cleaning_second.wait(), 5)
        await asyncio.wait_for(first_recorded.wait(), 5)
        release_cleanup.set()
        await asyncio.wait_for(cancelled, 5)
        assert cleaned == [1]
        assert (await store.get_status(job["id"]))["status"] == "cancelled"
    finally:
        release_cleanup.set()
        await jobs.close()
