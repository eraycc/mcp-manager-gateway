"""PyPI cache lifetime is configurable; only successful fetches advance it."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from mcp_manager.database import set_setting
from mcp_manager.updates import VERSION, read_update_state, update_status, write_update_state


async def seed(db, user, seconds, **extra):
    state = {"current_version": VERSION, "latest_version": "99.0.0",
             "checked_at": (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(), **extra}
    await write_update_state(db, user, state)
    return state


@pytest.mark.parametrize(("age", "interval", "fetches"), [
    (23 * 3600, None, 0), (25 * 3600, None, 1),
    (30, 60, 0), (61, 60, 1), (3600, 7200, 0),
])
async def test_auto_check_uses_configured_cache_lifetime(dual_db, monkeypatch, age, interval, fetches):
    if interval is not None:
        await set_setting(dual_db, "update_check_interval_seconds", interval)
    await seed(dual_db, "alice", age)
    calls = []
    async def fetch():
        calls.append(1)
        return {"version": "100.0.0", "pypi_url": "", "releases_url": ""}
    monkeypatch.setattr("mcp_manager.updates.fetch_latest_release", fetch)
    result = await update_status(dual_db, "alice")
    assert len(calls) == fetches
    assert result["latest_version"] == ("100.0.0" if fetches else "99.0.0")


async def test_cache_boundary_is_strictly_exceeded_and_local_upgrade_keeps_cache(dual_db, monkeypatch):
    from mcp_manager import updates

    fixed = datetime.now(UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    monkeypatch.setattr(updates, "datetime", Clock)
    await set_setting(dual_db, "update_check_interval_seconds", 60)
    await write_update_state(dual_db, "alice", {
        "current_version": "0.0.1", "latest_version": "99.0.0",
        "checked_at": (fixed - timedelta(seconds=60)).isoformat()})
    async def no_fetch():
        pytest.fail("An unexpired PyPI cache must be reused, including after a local upgrade")
    monkeypatch.setattr(updates, "fetch_latest_release", no_fetch)
    assert (await update_status(dual_db, "alice"))["latest_version"] == "99.0.0"


async def test_failure_does_not_advance_success_time_and_manual_always_fetches(dual_db, monkeypatch):
    state = await seed(dual_db, "alice", 25 * 3600)
    calls = []
    async def unavailable():
        calls.append(1)
        raise httpx.ConnectError("offline")
    monkeypatch.setattr("mcp_manager.updates.fetch_latest_release", unavailable)
    assert (await update_status(dual_db, "alice"))["check_error"]
    assert await read_update_state(dual_db, "alice") == state
    await update_status(dual_db, "alice")
    assert len(calls) == 2
    await seed(dual_db, "alice", 0)
    await update_status(dual_db, "alice", force=True)
    assert len(calls) == 3


async def test_interval_setting_is_validated_persisted_and_applied_immediately(db_app, monkeypatch):
    web = db_app.web
    assert (await web.get("/api/v1/settings")).json()["update_check_interval_seconds"] == 86400
    for bad in (0, -1, True, 1.5, "60", 2**31):
        response = await web.patch("/api/v1/settings", json={"update_check_interval_seconds": bad})
        assert response.status_code == 422, response.text
    assert (await web.patch("/api/v1/settings", json={"update_check_interval_seconds": 120})).status_code == 200
    assert (await web.get("/api/v1/settings")).json()["update_check_interval_seconds"] == 120
    user = (await web.get("/api/v1/me")).json()["id"]
    await seed(db_app.db, user, 60)
    calls = []
    async def fetch():
        calls.append(1)
        return {"version": "100.0.0", "pypi_url": "", "releases_url": ""}
    monkeypatch.setattr("mcp_manager.updates.fetch_latest_release", fetch)
    await web.get("/api/v1/update-status")
    assert not calls
    assert (await web.patch("/api/v1/settings", json={"update_check_interval_seconds": 30})).status_code == 200
    assert not calls  # Saving does not start a timer or network request.
    await web.get("/api/v1/update-status")
    assert len(calls) == 1
    await web.post("/api/v1/update-status/check")
    assert len(calls) == 2
    assert (await web.patch("/api/v1/settings", json={
        "update_auto_check": False, "update_check_interval_seconds": 600})).status_code == 200
    assert (await web.get("/api/v1/settings")).json()["update_check_interval_seconds"] == 600
    await seed(db_app.db, user, 3600)
    await web.get("/api/v1/update-status")
    assert len(calls) == 2
    await web.post("/api/v1/update-status/check")
    assert len(calls) == 3


async def test_cache_lifetime_starts_when_pypi_fetch_succeeds(dual_db, monkeypatch):
    from mcp_manager import updates

    ticks = [datetime.now(UTC)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return ticks[0]
    monkeypatch.setattr(updates, "datetime", Clock)
    await set_setting(dual_db, "update_check_interval_seconds", 1)
    calls = []
    async def slow_fetch():
        calls.append(1)
        ticks[0] += timedelta(seconds=10)
        return {"version": "100.0.0", "pypi_url": "", "releases_url": ""}
    monkeypatch.setattr(updates, "fetch_latest_release", slow_fetch)
    await update_status(dual_db, "alice")
    stored = await read_update_state(dual_db, "alice")
    assert stored["checked_at"] == ticks[0].isoformat()
    await update_status(dual_db, "alice")
    assert len(calls) == 1
