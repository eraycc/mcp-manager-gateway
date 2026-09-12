"""Missing service IDs must not produce successful actions or phantom holds."""
import asyncio

import pytest
from test_bug1_catalog import console


@pytest.mark.parametrize("action", ["stop", "test-all"])
async def test_single_action_rejects_missing_service_without_side_effects(tmp_path, action):
    async with console(tmp_path) as (app, web, _):
        jobs = set(app.state.jobs.items)
        response = await web.post("/api/v1/mcps/missing/" + action)
        assert response.status_code == 404, response.text
        assert "missing" not in app.state.runtime.holds
        assert set(app.state.jobs.items) == jobs


async def test_batch_stop_reports_missing_but_still_stops_existing_service(tmp_path):
    async with console(tmp_path) as (app, web, _):
        row = await app.state.catalog.create({"name": "Stopped service", "mode": "disabled",
                                              "config": {"command": "unused"}})
        response = await web.post("/api/v1/mcps/batch", json={"action": "stop", "ids": ["missing", row.id]})
        assert response.status_code == 200
        job = app.state.jobs.items[response.json()["id"]]
        async with asyncio.timeout(5):
            while job["status"] in {"queued", "running"}:
                await asyncio.sleep(.01)
        results = {item["item"]: item for item in job["results"]}
        assert results["missing"]["ok"] is False
        assert results[row.id]["ok"] is True
        assert job["status"] == "completed_with_errors"
        assert "missing" not in app.state.runtime.holds
        assert row.id in app.state.runtime.holds
        # An existing service already stopped remains an idempotent success.
        assert (await web.post("/api/v1/mcps/" + row.id + "/stop")).status_code == 200
