import asyncio

import pytest

from mcp_manager.jobs import Jobs


@pytest.mark.asyncio
async def test_cancelled_item_awaits_sibling_cleanup_and_records_each_outcome():
    jobs = Jobs()
    started = asyncio.Event()
    finish = asyncio.Event()
    cleaned = asyncio.Event()

    async def operation(item):
        if item == "cancel":
            await started.wait()
            raise asyncio.CancelledError()
        started.set()
        try:
            await finish.wait()
        finally:
            cleaned.set()

    job = jobs.submit("test", ["cancel", "blocked"], operation)
    task = jobs.tasks[job["id"]]
    try:
        await asyncio.wait_for(task, 1)
        assert cleaned.is_set()
        assert job["status"] == "cancelled"
        assert job["completed"] == len(job["results"]) == 2
        assert {item["item"] for item in job["results"]} == {"cancel", "blocked"}
        assert all(item["cancelled"] and not item["ok"] for item in job["results"])
        assert jobs.tasks == {}
    finally:
        finish.set()
        await jobs.close()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_shutdown_records_cancelled_items_including_semaphore_waiters():
    jobs = Jobs()
    entered = asyncio.Event()
    async def operation(item):
        entered.set()
        await asyncio.Event().wait()

    job = jobs.submit("test", list(range(8)), operation)
    await entered.wait()
    await jobs.close()
    assert job["status"] == "cancelled"
    assert job["completed"] == len(job["results"]) == job["total"] == 8
    assert all(item["cancelled"] and not item["ok"] for item in job["results"])
    assert jobs.tasks == {}


@pytest.mark.asyncio
async def test_shutdown_before_job_starts_records_all_cancelled_items():
    jobs = Jobs()
    async def operation(item):
        pytest.fail("Queued operation must not run after shutdown")

    job = jobs.submit("test", ["a", "a"], operation)
    await jobs.close()
    assert job["status"] == "cancelled"
    assert job["completed"] == len(job["results"]) == 2
    assert all(item["cancelled"] for item in job["results"])
    assert jobs.tasks == {}


@pytest.mark.asyncio
async def test_finished_jobs_release_task_handles_but_keep_results():
    jobs = Jobs()
    async def operation(item):
        if item == "bad":
            raise ValueError("invalid item")
        return {"value": item}

    job = jobs.submit("test", ["ok", "bad"], operation)
    await jobs.tasks[job["id"]]
    assert jobs.tasks == {}
    assert jobs.items[job["id"]] is job
    assert job["status"] == "completed_with_errors"
    assert job["completed"] == len(job["results"]) == 2
    assert job["results"] == [
        {"item": "ok", "ok": True, "result": {"value": "ok"}},
        {"item": "bad", "ok": False, "error": "invalid item"},
    ]
