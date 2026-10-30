"""Cancelling a worker during an active child records uncertainty and exits once."""
import asyncio


async def test_worker_shutdown_does_not_swallow_its_own_cancellation(db_app):
    jobs = db_app.app.state.jobs
    entered, release = asyncio.Event(), asyncio.Event()
    async def operation(_):
        entered.set()
        await release.wait()
        return {"done": True}
    status = await jobs.submit("test.shutdown", ["one"], operation)
    await asyncio.wait_for(entered.wait(), 5)
    workers = list(jobs.workers)
    for worker in workers:
        worker.cancel()
    try:
        _done, pending = await asyncio.wait(workers, timeout=2)
        assert not pending, "A worker consumed shutdown cancellation and waited for new jobs forever"
        assert all(worker.cancelled() for worker in workers)
        final = await jobs.store.get_status(status["id"])
        assert final["status"] == "cancelled"
        result = await jobs.store.get_result(status["id"], 0)
        assert not result["ok"] and result["outcome_unknown"]
    finally:
        release.set()
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
