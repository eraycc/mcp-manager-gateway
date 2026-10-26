"""Maintenance refresh recovers stopped or failed enabled services."""
from contextlib import asynccontextmanager

import pytest
from test_bug1_catalog import Connection, console

from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize("entry", ["scheduled", "manual", "legacy"])
@pytest.mark.parametrize("state", ["stopped", "failed"])
async def test_refresh_recovers_enabled_service_and_publishes_current_catalog(tmp_path, mode, entry, state):
    async with console(tmp_path) as (app, web, _actor):
        tool = ["before"]
        catalog, runtime = app.state.catalog, app.state.runtime

        @asynccontextmanager
        async def connect(spec):
            yield Connection(tool[0])

        runtime.connector = connect
        row = await catalog.create({"name": "recover", "mode": mode, "config": {"command": "fake"}})
        before = (await web.get("/api/v1/mcps")).json()["items"][0]["cache_at"]
        response = await web.post(f"/api/v1/mcps/{row.id}/stop")
        assert response.status_code == 200
        if state == "failed":
            for _ in range(3):
                await catalog.record_start_failure(row, GatewayError("startup_failed", "offline"))
            assert catalog.cached(row)["runtime_status"] == "failed"
        tool[0] = "recovered"
        if entry == "scheduled":
            target = {"server_id": row.id, "user_id": None}
            assert target in await catalog.refresh_targets()
            await catalog.refresh_target(target)
        elif entry == "manual":
            response = await web.post(f"/api/v1/mcps/{row.id}/refresh")
            assert response.status_code == 200, response.text
        else:
            result = await catalog.refresh(row.id)
            assert result["tools"][0]["name"] == "recovered"
        cache = catalog.cached(row)
        assert cache["cache_status"] == "ready"
        assert cache["startup_failure_count"] == 0
        assert cache["last_refresh_error_code"] is None
        assert (await catalog.get(row.id)).mode == mode
        if entry != "legacy":
            item = (await web.get("/api/v1/mcps")).json()["items"][0]
            assert item["cache_at"] > before
            capabilities = (await web.get(f"/api/v1/mcps/{row.id}/capabilities")).json()
            assert [item["name"] for item in capabilities["items"]] == ["recovered"]
        lease = runtime.create_lease("system", "business")
        try:
            spec = await catalog.spec(row)
            assert (await runtime.call(spec, lease.id, "recovered", {}))["content"][0]["text"] == "ok"
        finally:
            await runtime.release(lease.id)
        assert not runtime.leases


async def test_disabled_refresh_never_connects(tmp_path):
    async with console(tmp_path) as (app, _web, _actor):
        row = await app.state.catalog.create({
            "name": "disabled", "mode": "disabled", "config": {"command": "never"},
        })
        assert await app.state.catalog.refresh_targets() == []
        for refresh in (app.state.catalog.refresh, app.state.catalog.refresh_index):
            with pytest.raises(GatewayError) as disabled:
                await refresh(row.id)
            assert disabled.value.code == "disabled"
        assert not app.state.runtime.status()


async def test_stopped_refresh_failure_retains_catalog_until_threshold(tmp_path, monkeypatch):
    async with console(tmp_path) as (app, web, _actor):
        @asynccontextmanager
        async def connect(spec):
            yield Connection("cached")

        catalog = app.state.catalog
        app.state.runtime.connector = connect
        row = await catalog.create({"name": "cached", "config": {"command": "fake"}})
        async def stopped(*args, **kwargs):
            raise GatewayError("stopped", "MCP stopped during refresh")

        monkeypatch.setattr(app.state.runtime, "perform", stopped)
        with pytest.raises(GatewayError) as failure:
            await catalog.refresh_index(row.id)
        assert failure.value.code == "stopped"
        item = (await web.get("/api/v1/mcps")).json()["items"][0]
        assert item["cache_at"] is not None
        assert item["tool_count"] == 1 and item["cache_status"] == "ready"
        assert item["startup_failure_count"] == 1 and item["status"] == "stopped"
        assert item["last_refresh_error_code"] == "stopped"
        assert item["last_refresh_failure_at"]
        assert item["last_refresh_error"] == "MCP stopped during refresh"


async def test_cron_refresh_runs_at_configured_timezone_and_publishes_snapshot(tmp_path, monkeypatch):
    import asyncio
    import importlib
    from datetime import UTC, datetime, timedelta

    from mcp_manager.database import set_setting

    app_module = importlib.import_module("mcp_manager.app")
    async with console(tmp_path) as (app, web, _actor):
        tool = ["before"]

        @asynccontextmanager
        async def connect(spec):
            yield Connection(tool[0])

        app.state.runtime.connector = connect
        row = await app.state.catalog.create({"name": "cron", "config": {"command": "fake"}})
        response = await web.post(f"/api/v1/mcps/{row.id}/stop")
        assert response.status_code == 200
        await app.state.catalog.create({
            "name": "disabled", "mode": "disabled", "config": {"command": "never"},
        })
        await set_setting(app.state.db, "refresh_enabled", True)
        await set_setting(app.state.db, "refresh_cron", "0 3 * * *")
        await set_setting(app.state.db, "timezone", "Asia/Shanghai")
        current = [datetime(2026, 10, 17, 18, 59, tzinfo=UTC)]
        monkeypatch.setattr(app_module, "now", lambda: current[0])
        app.state.refresh_next = None
        await app_module._scheduled_refresh(app)
        assert app.state.refresh_next == datetime(2026, 10, 17, 19, tzinfo=UTC)
        assert not app.state.jobs.tasks
        tool[0] = "cron-updated"
        current[0] = app.state.refresh_next + timedelta(seconds=1)
        await app_module._scheduled_refresh(app)
        identity, task = next(iter(app.state.jobs.tasks.items()))
        await asyncio.wait_for(task, 5)
        job = await app.state.jobs.store.get_status(identity)
        assert job["status"] == "completed"
        assert job["total"] == 1
        capabilities = (await web.get(f"/api/v1/mcps/{row.id}/capabilities")).json()
        assert [item["name"] for item in capabilities["items"]] == ["cron-updated"]
        assert app.state.refresh_next == datetime(2026, 10, 18, 19, tzinfo=UTC)

