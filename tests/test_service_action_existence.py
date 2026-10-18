"""Missing service IDs must not produce successful actions or runtime side effects."""
import asyncio
from contextlib import asynccontextmanager

import pytest
from test_bug1_catalog import Connection, console


@pytest.mark.parametrize("action", ["stop", "test-all"])
async def test_single_action_rejects_missing_service_without_side_effects(tmp_path, action):
    async with console(tmp_path) as (app, web, _):
        jobs = (await app.state.jobs.store.list_jobs())["total"]
        instances = app.state.runtime.status()
        leases = set(app.state.runtime.leases)
        response = await web.post("/api/v1/mcps/missing/" + action)
        assert response.status_code == 404, response.text
        assert app.state.runtime.status() == instances
        assert set(app.state.runtime.leases) == leases
        assert (await app.state.jobs.store.list_jobs())["total"] == jobs


async def test_batch_stop_reports_missing_but_still_stops_existing_service(tmp_path):
    async with console(tmp_path) as (app, web, _):
        @asynccontextmanager
        async def connect(spec):
            yield Connection()

        app.state.runtime.connector = connect
        row = await app.state.catalog.create({
            "name": "Stopped service", "mode": "lazy", "config": {"command": "unused"},
        })
        lease = app.state.runtime.create_lease("system", "test")
        try:
            await app.state.catalog.call(row, lease, "first", {"value": "before"})
            assert app.state.runtime.status()[0]["phase"] == "ready"
            response = await web.post("/api/v1/mcps/batch", json={
                "action": "stop", "ids": ["missing", row.id],
            })
            assert response.status_code == 200
            job_id = response.json()["id"]
            task = app.state.jobs.tasks.get(job_id)
            if task is not None:
                await asyncio.wait_for(task, 5)
            job = await app.state.jobs.store.get_status(job_id)
            results = {
                item["item"]: item
                for item in [
                    await app.state.jobs.store.get_result(job_id, 0),
                    await app.state.jobs.store.get_result(job_id, 1),
                ]
            }
            assert results["missing"]["ok"] is False
            assert results[row.id]["ok"] is True
            assert job["status"] == "completed_with_errors"
            assert app.state.runtime.status() == []
            assert (await app.state.catalog.get(row.id)).mode == "lazy"
            tools = (await web.get(f"/api/v1/mcps/{row.id}/capabilities")).json()
            assert [item["name"] for item in tools["items"]] == ["first"]
            # Stop remains idempotent and does not block a subsequent business call.
            assert (await web.post(f"/api/v1/mcps/{row.id}/stop")).status_code == 200
            result = await app.state.catalog.call(row, lease, "first", {"value": "after"})
            assert result["content"][0]["text"] == "ok"
        finally:
            await app.state.runtime.release(lease.id)
