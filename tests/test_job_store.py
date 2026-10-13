"""Persistent, fixed-size progress and unique append-only job results."""
import json

from sqlalchemy import func, select


async def test_progress_is_bounded_results_append_once_and_restart_never_replays(db_app):
    from mcp_manager.job_models import JobResult
    from mcp_manager.job_store import JobStore
    store = JobStore(db_app.db, db_app.app.state.catalog.artifacts)
    job = await store.create_job("fixture", 50, user_id=db_app.actor["id"])
    await store.set_status(job["id"], "running")
    for index in range(50):
        result = {"item": index, "ok": True, "result": {"text": "x" * 5000}}
        assert await store.append_result(job["id"], index, result)
        assert not await store.append_result(job["id"], index, result)
    status = await store.get_status(job["id"])
    assert len(json.dumps(status).encode()) <= 16384
    assert "results" not in status and status["completed"] == status["succeeded"] == 50
    page = await store.list_results(job["id"], limit=20)
    assert page["total"] == 50 and len(page["items"]) == 20 and page["next_cursor"]
    async with db_app.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(JobResult)) == 50
    assert (await store.get_result(job["id"], 0))["result"]["text"] == "x" * 5000
    await store.interrupt_unfinished()
    assert (await store.get_status(job["id"]))["status"] == "interrupted"


async def test_job_http_contract_keeps_status_small_and_pages_results(db_app):
    from mcp_manager.job_store import JobStore

    store = JobStore(db_app.db, db_app.app.state.catalog.artifacts)
    job = await store.create_job("fixture", 2, user_id=db_app.actor["id"])
    assert await store.append_result(job["id"], 0, {"ok": True, "value": "first"})
    assert await store.append_result(job["id"], 1, {"ok": True, "value": "second"})

    status = await db_app.web.get("/api/v1/jobs/" + job["id"])
    assert status.status_code == 200
    assert "results" not in status.json()
    etag = status.headers["etag"]
    unchanged = await db_app.web.get(
        "/api/v1/jobs/" + job["id"], headers={"If-None-Match": etag}
    )
    assert unchanged.status_code == 304

    page = await db_app.web.get("/api/v1/jobs/" + job["id"] + "/results?limit=1")
    assert page.status_code == 200
    assert len(page.json()["items"]) == 1 and page.json()["next_cursor"]
    detail = await db_app.web.get("/api/v1/jobs/" + job["id"] + "/results/0")
    assert detail.json()["value"] == "first"


async def test_legacy_background_progress_exhibits_unbounded_result_payload(db_app):
    jobs = db_app.app.state.jobs
    async def operation(item):
        return {"text": "x" * 5000}
    job = await jobs.submit("fixture", list(range(50)), operation)
    await jobs.tasks[job["id"]]
    job = await jobs.store.get_status(job["id"])
    assert len(json.dumps(job).encode()) <= 16384
    assert "results" not in job

