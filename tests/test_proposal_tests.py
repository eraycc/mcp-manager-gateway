import asyncio

import httpx
import pytest
from sqlalchemy import select

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import ApiToken, McpProposal, User
from mcp_manager.proposals import submit_proposals
from mcp_manager.runtime import GatewayError


@pytest.fixture
async def proposal_web(tmp_path):
    app = create_app(Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path}/proposal-tests.db",
        secret_key="proposal-tests-key",
        public_url="http://test",
    ))
    ready, finished = asyncio.Event(), asyncio.Event()

    async def lifespan_owner():
        async with app.router.lifespan_context(app):
            ready.set()
            await finished.wait()

    owner = asyncio.create_task(lifespan_owner())
    await asyncio.wait_for(ready.wait(), 10)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/v1/auth/register", json={
                "username": "admin", "password": "password12345",
            })
            await client.post("/api/v1/auth/login", json={
                "username": "admin", "password": "password12345",
            })
            client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]
            created = await client.post("/api/v1/tokens", json={
                "name": "proposal-tests", "scope_mode": "all",
                "enable_mcp_proposal": True,
            })
            async with app.state.db.session() as session:
                user = await session.scalar(select(User).where(User.username == "admin"))
                token = await session.get(ApiToken, created.json()["id"])

            async def submit(name, command):
                result = await submit_proposals(app, user, token, {
                    "name": name,
                    "transport": "stdio",
                    "config": {"command": command},
                    "purpose": "proposal test",
                })
                return result["items"][0]["id"]

            yield app, client, submit
    finally:
        finished.set()
        await asyncio.wait_for(owner, 10)


@pytest.mark.asyncio
async def test_single_test_success_failure_and_retest_are_persisted(proposal_web, monkeypatch):
    app, client, submit = proposal_web
    success_id = await submit("Success", "success")
    failure_id = await submit("Failure", "failure")

    async def discover(spec, lease_id):
        if spec.config["command"] == "failure":
            raise GatewayError("connection_error", "fixture connection failed")
        return {
            "tools": [{"name": "one"}, {"name": "two"}],
            "resources": [{"uri": "fixture://resource"}],
            "prompts": [{"name": "prompt"}],
            "templates": [{"uriTemplate": "fixture://{id}"}],
        }

    monkeypatch.setattr(app.state.runtime, "discover", discover)
    response = await client.post(f"/api/v1/mcp-proposals/{success_id}/test")
    assert response.status_code == 200, response.text
    assert response.json()["tool_count"] == 2

    response = await client.post(f"/api/v1/mcp-proposals/{failure_id}/test")
    assert response.status_code == 502, response.text
    listing = (await client.get("/api/v1/mcp-proposals")).json()["items"]
    success = next(item for item in listing if item["id"] == success_id)
    failure = next(item for item in listing if item["id"] == failure_id)
    assert success["test_status"] == "success"
    assert success["test_result"] == {
        "tool_count": 2,
        "resource_count": 1,
        "prompt_count": 1,
        "template_count": 1,
    }
    assert success["test_error"] == ""
    assert success["tested_at"]
    assert failure["test_status"] == "failed"
    assert "fixture connection failed" in failure["test_error"]
    assert failure["test_result"] == {}
    failed_at = failure["tested_at"]

    async def recovered(spec, lease_id):
        return {"tools": [{"name": "recovered"}], "resources": [], "prompts": [], "templates": []}

    monkeypatch.setattr(app.state.runtime, "discover", recovered)
    response = await client.post(f"/api/v1/mcp-proposals/{failure_id}/test")
    assert response.status_code == 200, response.text
    detail = (await client.get(f"/api/v1/mcp-proposals/{failure_id}")).json()
    assert detail["test_status"] == "success"
    assert detail["test_result"]["tool_count"] == 1
    assert detail["test_error"] == ""
    assert detail["tested_at"] >= failed_at


@pytest.mark.asyncio
async def test_batch_test_reports_incremental_results_and_can_be_cancelled(
    proposal_web, monkeypatch
):
    app, client, submit = proposal_web
    good_id = await submit("Good batch", "good")
    bad_id = await submit("Bad batch", "bad")

    async def discover(spec, lease_id):
        await asyncio.sleep(0)
        if spec.config["command"] == "bad":
            raise GatewayError("connection_error", "batch failed")
        return {"tools": [{"name": "ok"}], "resources": [], "prompts": [], "templates": []}

    monkeypatch.setattr(app.state.runtime, "discover", discover)
    response = await client.post("/api/v1/mcp-proposals/batch-test", json={
        "ids": [good_id, bad_id],
    })
    assert response.status_code == 200, response.text
    job = response.json()
    task = app.state.jobs.tasks[job["id"]]
    await asyncio.wait_for(task, 5)
    saved_job = (await client.get("/api/v1/jobs/" + job["id"])).json()
    assert saved_job["status"] == "completed_with_errors"
    assert saved_job["completed"] == saved_job["total"] == 2
    assert {item["item"] for item in saved_job["results"]} == {good_id, bad_id}

    rows = (await client.get("/api/v1/mcp-proposals")).json()["items"]
    statuses = {item["id"]: item["test_status"] for item in rows}
    assert statuses == {good_id: "success", bad_id: "failed"}

    cancel_a = await submit("Cancel A", "wait-a")
    cancel_b = await submit("Cancel B", "wait-b")
    entered = asyncio.Event()

    async def waiting(spec, lease_id):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.runtime, "discover", waiting)
    response = await client.post("/api/v1/mcp-proposals/batch-test", json={
        "ids": [cancel_a, cancel_b],
    })
    job = response.json()
    task = app.state.jobs.tasks[job["id"]]
    await asyncio.wait_for(entered.wait(), 5)
    cancelled = await client.post("/api/v1/jobs/" + job["id"] + "/cancel")
    assert cancelled.status_code == 200, cancelled.text
    await asyncio.gather(task, return_exceptions=True)

    rows = (await client.get("/api/v1/mcp-proposals")).json()["items"]
    statuses = {item["id"]: item["test_status"] for item in rows}
    assert statuses[cancel_a] == "cancelled"
    assert statuses[cancel_b] == "cancelled"


@pytest.mark.asyncio
async def test_batch_test_requires_administrator(proposal_web):
    app, _client, submit = proposal_web
    proposal_id = await submit("Forbidden", "never")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ordinary:
        await ordinary.post("/api/v1/auth/register", json={
            "username": "ordinary", "password": "password12345",
        })
        await ordinary.post("/api/v1/auth/login", json={
            "username": "ordinary", "password": "password12345",
        })
        ordinary.headers["X-CSRF-Token"] = ordinary.cookies["mcp_csrf"]
        response = await ordinary.post("/api/v1/mcp-proposals/batch-test", json={
            "ids": [proposal_id],
        })
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_batch_test_revalidates_admin_before_queued_execution(proposal_web):
    app, client, submit = proposal_web
    proposal_id = await submit("Revoked", "must-not-run")
    app.state.jobs.limit = asyncio.Semaphore(0)
    response = await client.post("/api/v1/mcp-proposals/batch-test", json={
        "ids": [proposal_id],
    })
    assert response.status_code == 200, response.text
    job = response.json()
    task = app.state.jobs.tasks[job["id"]]
    async with app.state.db.locked() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        user.auth_version += 1
    app.state.jobs.limit.release()
    await asyncio.wait_for(task, 5)
    saved = app.state.jobs.items[job["id"]]
    assert saved["status"] == "completed_with_errors"
    assert "Session revoked" in saved["results"][0]["error"]
    async with app.state.db.session() as session:
        proposal = await session.get(McpProposal, proposal_id)
        assert proposal.test_status == "pending"
