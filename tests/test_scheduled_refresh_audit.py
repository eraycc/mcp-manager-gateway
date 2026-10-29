"""Scheduled catalog refreshes audit each actual target without changing job outcomes."""
import asyncio
from datetime import timedelta

import pytest
from test_consecutive_operation_failures import healthy_service

from mcp_manager import app as app_module
from mcp_manager.database import set_setting

ACTION = "mcp.refresh"


async def schedule(h):
    state = h.app.state
    await set_setting(h.db, "refresh_enabled", True)
    state.refresh_next = app_module.now() - timedelta(seconds=1)
    await app_module._scheduled_refresh(h.app)
    identity, task = next(iter(state.jobs.tasks.items()))
    await asyncio.wait_for(task, 15)
    return await state.jobs.store.get_status(identity)


async def events(h):
    response = await h.web.get("/api/v1/audit-logs", params={"action": ACTION, "source": "system"})
    assert response.status_code == 200, response.text
    return response.json()["items"]


@pytest.mark.parametrize("isolation", ["service", "user"])
async def test_scheduled_refresh_audits_object_owner_counts_and_system_actor(db_app, isolation):
    row, _faults, _context = await healthy_service(db_app, "oauth" if isolation == "user" else "http", isolation)
    job = await schedule(db_app)
    assert job["succeeded"] == 1 and job["failed"] == 0
    records = await events(db_app)
    assert len(records) == 1
    event = records[0]
    assert event["target_type"] == "mcp" and event["target_id"] == row.id
    assert event["target_name"] == row.name
    assert event["actor_type"] == event["source"] == "system" and event["user_id"] is None
    assert event["result"] == "success"
    detail = (await db_app.web.get("/api/v1/audit-logs/" + event["event_id"])).json()["details"]
    assert detail["owner_id"] == (db_app.actor["id"] if isolation == "user" else "service")
    assert detail["trigger"] == "scheduled"
    assert detail["cache_status"] == "ready"
    assert [detail[key] for key in ("tool_count", "resource_count", "prompt_count", "template_count")] == [1] * 4
    actions = (await db_app.web.get("/api/v1/audit-logs/actions")).json()["items"]
    assert any(item["action"] == ACTION for item in actions)
    assert (await db_app.web.post(f"/api/v1/mcps/{row.id}/refresh")).status_code == 200
    assert len(await events(db_app)) == 1  # Manual refresh keeps its existing event.


@pytest.mark.parametrize("isolation", ["service", "user"])
async def test_scheduled_failure_is_audited_even_when_last_catalog_is_retained(db_app, isolation):
    row, faults, _context = await healthy_service(db_app, "oauth" if isolation == "user" else "http", isolation)
    faults["discovery"] = True
    job = await schedule(db_app)
    assert job["failed"] == 1 and job["succeeded"] == 0
    cache = db_app.app.state.catalog.cached(row, db_app.actor["id"])
    assert cache["startup_failure_count"] == 1 and cache["cache_stale"]
    records = await events(db_app)
    assert len(records) == 1 and records[0]["result"] == "failure"
    detail = (await db_app.web.get("/api/v1/audit-logs/" + records[0]["event_id"])).json()["details"]
    assert detail["reason_code"] == "discovery_failed"
    assert detail["cache_status"] == "ready" and detail["cache_stale"] is True


async def test_audit_write_failure_does_not_turn_scheduled_refresh_into_failure(db_app, monkeypatch):
    await healthy_service(db_app, "http")
    def fail(_event):
        raise OSError("simulated full audit disk")
    monkeypatch.setattr(db_app.app.state.logs.audits, "_write", fail)
    job = await schedule(db_app)
    assert job["succeeded"] == 1 and job["failed"] == 0
    assert (await db_app.app.state.logs.audits.status())["write_failures"] >= 1


async def test_cancelled_scheduled_refresh_is_never_audited_as_success(db_app, monkeypatch):
    row, _faults, _context = await healthy_service(db_app, "http")
    entered = asyncio.Event()
    async def hang(_target):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(db_app.app.state.catalog, "refresh_target", hang)
    state = db_app.app.state
    await set_setting(db_app.db, "refresh_enabled", True)
    state.refresh_next = app_module.now() - timedelta(seconds=1)
    await app_module._scheduled_refresh(db_app.app)
    identity = next(iter(state.jobs.tasks))
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(state.jobs.cancel(identity), 5)
    records = await events(db_app)
    assert len(records) == 1 and records[0]["target_id"] == row.id
    assert records[0]["result"] == "unknown"
    detail = (await db_app.web.get("/api/v1/audit-logs/" + records[0]["event_id"])).json()["details"]
    assert detail["reason_code"] == "cancelled"
