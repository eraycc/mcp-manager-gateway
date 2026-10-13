"""Old discoveries and cancellations must never update a newer proposal test."""
import asyncio

from test_proposal_atomicity import propose


async def test_changed_configuration_cannot_publish_old_success(db_app, monkeypatch):
    identity = await propose(db_app)
    entered, release = asyncio.Event(), asyncio.Event()
    async def discover(spec, lease):
        entered.set()
        await release.wait()
        return {"tools": [{"name": "old-tool"}], "resources": [], "templates": [], "prompts": []}
    monkeypatch.setattr(db_app.app.state.runtime, "discover", discover)
    task = asyncio.create_task(db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        response = await db_app.web.patch(f"/api/v1/mcp-proposals/{identity}", json={
            "config": {"command": "new-synthetic-never-launched"}, "mode": "lazy", "isolation": "service"
        })
        assert response.status_code == 200
    finally:
        release.set()
    tested = await asyncio.wait_for(task, 5)
    detail = (await db_app.web.get(f"/api/v1/mcp-proposals/{identity}")).json()
    assert detail["test_status"] != "success", "Old configuration was not tested"
    assert tested.json().get("status") == "superseded"
    assert detail["test_result"] == {}


async def test_older_run_cannot_replace_newer_run_success(db_app, monkeypatch):
    identity = await propose(db_app)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def discover(spec, lease):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return {"tools": [{"name": "old-a"}, {"name": "old-b"}]}
        return {"tools": [{"name": "new"}]}
    monkeypatch.setattr(db_app.app.state.runtime, "discover", discover)
    old = asyncio.create_task(db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        newer = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test")
        assert newer.status_code == 200
    finally:
        release.set()
    stale = await asyncio.wait_for(old, 5)
    detail = (await db_app.web.get(f"/api/v1/mcp-proposals/{identity}")).json()
    assert detail["test_result"]["tool_count"] == 1
    assert stale.json().get("status") == "superseded"


async def test_cancel_older_run_does_not_cancel_new_run(db_app, monkeypatch):
    identity = await propose(db_app)
    entered = asyncio.Event()
    calls = 0
    async def discover(spec, lease):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.Event().wait()
        return {"tools": [{"name": "new"}]}
    monkeypatch.setattr(db_app.app.state.runtime, "discover", discover)
    old = asyncio.create_task(db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test"))
    await asyncio.wait_for(entered.wait(), 5)
    await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test")
    old.cancel()
    await asyncio.gather(old, return_exceptions=True)
    detail = (await db_app.web.get(f"/api/v1/mcp-proposals/{identity}")).json()
    assert detail["test_status"] == "success"


async def test_finalized_proposal_cannot_receive_test_writeback(db_app, monkeypatch):
    identity = await propose(db_app)
    entered, release = asyncio.Event(), asyncio.Event()
    async def discover(spec, lease):
        entered.set()
        await release.wait()
        return {"tools": [{"name": "old"}]}
    monkeypatch.setattr(db_app.app.state.runtime, "discover", discover)
    old = asyncio.create_task(db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        response = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/reject", json={"reason": "No"})
        assert response.status_code == 200
    finally:
        release.set()
    stale = await asyncio.wait_for(old, 5)
    assert stale.json().get("status") == "superseded"
    detail = (await db_app.web.get(f"/api/v1/mcp-proposals/{identity}")).json()
    assert detail["test_status"] != "success"
