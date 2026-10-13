"""Cancellation and completion semantics for the fixed worker pool."""
import asyncio

from mcp_manager.jobs import Jobs


async def test_cancelled_item_awaits_sibling_cleanup_and_records_each_outcome(db_app):
    jobs = db_app.app.state.jobs
    started, finish, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def operation(item):
        if item == "cancel":
            await started.wait()
            raise asyncio.CancelledError()
        started.set()
        try:
            await finish.wait()
        finally:
            cleaned.set()
    status = await jobs.submit("test", ["cancel", "blocked"], operation, db_app.actor["id"])
    future = jobs.tasks[status["id"]]
    try:
        await asyncio.wait_for(future, 5)
        assert cleaned.is_set()
        current = await jobs.store.get_status(status["id"])
        assert current["status"] == "cancelled" and current["completed"] == 2
        page = await jobs.store.list_results(status["id"])
        assert {item["item"] for item in page["items"]} == {'"cancel"', '"blocked"'}
        assert all(item["cancelled"] and not item["ok"] for item in page["items"])
    finally:
        finish.set()


async def test_shutdown_records_waiting_items_without_starting_them(db_app):
    # Use a second pool over the same persistent store so app teardown ownership is unchanged.
    jobs = Jobs(store=db_app.app.state.jobs.store)
    await jobs.start()
    entered = asyncio.Event()
    calls = []
    async def operation(item):
        calls.append(item)
        entered.set()
        await asyncio.Event().wait()
    status = await jobs.submit("test", list(range(8)), operation, db_app.actor["id"])
    await entered.wait()
    closer = asyncio.create_task(jobs.close())
    try:
        await asyncio.wait_for(asyncio.shield(closer), 5)
    except TimeoutError:
        context = jobs.contexts.get(status["id"])
        evidence = {
            "queued": jobs.queued_items, "active_users": dict(jobs.active_users),
            "remaining": context.remaining if context else None,
            "active": list(context.active) if context else None,
            "workers": [(task.done(), task._state) for task in jobs.workers],
            "status": await jobs.store.get_status(status["id"]),
        }
        closer.cancel()
        for worker in jobs.workers:
            worker.cancel()
        await asyncio.gather(closer, *jobs.workers, return_exceptions=True)
        raise AssertionError(evidence) from None
    current = await jobs.store.get_status(status["id"])
    assert current["status"] == "cancelled" and current["completed"] == 8
    assert len(calls) <= 2  # per-user active budget; waiters were not dispatched.


async def test_shutdown_before_dispatch_never_runs_operation(db_app):
    jobs = Jobs(store=db_app.app.state.jobs.store)
    async def operation(item):
        raise AssertionError("Queued operation must not run after shutdown")
    status = await jobs.submit("test", ["a", "a"], operation, db_app.actor["id"])
    await jobs.close()
    current = await jobs.store.get_status(status["id"])
    assert current["status"] == "cancelled" and current["completed"] == 2


async def test_finished_job_releases_handle_and_keeps_downloadable_results(db_app):
    jobs = db_app.app.state.jobs
    async def operation(item):
        if item == "bad":
            raise ValueError("invalid item")
        return {"value": item}
    status = await jobs.submit("test", ["ok", "bad"], operation, db_app.actor["id"])
    await jobs.tasks[status["id"]]
    assert status["id"] not in jobs.tasks
    current = await jobs.store.get_status(status["id"])
    assert current["status"] == "completed_with_errors" and current["completed"] == 2
    assert (await jobs.store.get_result(status["id"], 0))["result"] == {"value": "ok"}
    assert (await jobs.store.get_result(status["id"], 1))["error"] == "invalid item"
