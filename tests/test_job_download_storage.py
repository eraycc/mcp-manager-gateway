"""Authorized persistent result download follows its durable CAS namespace."""
async def test_persistent_result_download_streams_original_body(db_app):
    state = db_app.app.state
    job = await state.jobs.store.create_job("download", 1, user_id=db_app.actor["id"])
    body = {"value": "x" * 100000}
    await state.jobs.store.append_result(job["id"], 0, {"ok": True, "result": body})
    response = await db_app.web.get(f"/api/v1/jobs/{job['id']}/results/0/download")
    assert response.status_code == 200
    assert response.json()["result"] == body
    assert response.headers["cache-control"] == "private, no-store"
