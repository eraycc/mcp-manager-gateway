"""Jobs keep small status documents and page/download large results."""


async def test_jobs_contract(db_app, api_paths):
    assert "get" in api_paths["/api/v1/jobs"]
    assert "get" in api_paths["/api/v1/jobs/{job_id}/results"]
    assert "get" in api_paths["/api/v1/jobs/{job_id}/results/{ordinal}/download"]
    response = await db_app.web.get("/api/v1/jobs", params={"page": 1, "page_size": 1})
    assert response.status_code == 200
    assert len(response.content) <= 16_384
