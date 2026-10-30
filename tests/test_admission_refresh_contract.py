"""Runtime observations and failed directory refreshes never disable an enabled MCP."""
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from test_bug1_catalog import console
from test_discovery_failure_policy import Upstream

from mcp_manager.catalog_control import CatalogControl
from mcp_manager.catalog_store import scope_key
from mcp_manager.runtime import GatewayError


async def agent(app, web):
    created = await web.post("/api/v1/tokens", json={
        "name": "contract", "discovery_mode": "discovery", "scope_mode": "all",
    })
    request = Request({
        "type": "http", "method": "POST", "path": "/mcp", "app": app,
        "headers": [(b"authorization", ("Bearer " + created.json()["token"]).encode())],
    })

    async def call(name, arguments):
        return await app.state.gateway.call_tool(
            SimpleNamespace(request=request), SimpleNamespace(name=name, arguments=arguments),
        )

    return call


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize("entry", ["refresh", "refresh_index"])
@pytest.mark.parametrize("failure", ["connection", "discovery"])
async def test_failed_refresh_retains_views_until_threshold_without_disabling(
    tmp_path, mode, entry, failure,
):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        catalog, runtime = app.state.catalog, app.state.runtime
        runtime.connector = peer.connect
        row = await catalog.create({
            "name": "contract", "slug": "contract", "mode": mode, "config": {"command": "fake"},
        })
        await runtime.stop_server(row.id, force=True)
        if failure == "connection":
            peer.failure = OSError("upstream temporarily offline")
        else:
            peer.discovery_failure = RuntimeError("tools/list temporarily failed")
        reason = "upstream temporarily offline" if failure == "connection" else "tools/list temporarily failed"
        for count in (1, 2):
            with pytest.raises(GatewayError):
                await getattr(catalog, entry)(row.id)
            retained = (await web.get("/api/v1/mcps")).json()["items"][0]
            assert retained["cache_status"] == "ready" and retained["tool_count"] == 1
            assert retained["startup_failure_count"] == count and retained["cache_stale"]
            assert reason in retained["cache_error"]
            page = (await web.get(f"/api/v1/mcps/{row.id}/capabilities")).json()
            assert page["total"] == 1
        with pytest.raises(GatewayError):
            await getattr(catalog, entry)(row.id)
        assert (await catalog.get(row.id)).mode == mode
        assert (await catalog.spec(row)).mode == mode
        cache = catalog.cached(row)
        assert cache["tools"] == []
        assert cache["cache_status"] == "error"
        assert reason in cache["cache_error"]
        assert cache["last_refresh_error_code"]
        assert cache["last_refresh_failure_at"]
        item = (await web.get("/api/v1/mcps")).json()["items"][0]
        assert item["tool_count"] == 0 and item["cache_status"] == "error"
        assert reason in item["cache_error"]
        capabilities = (await web.get(f"/api/v1/mcps/{row.id}/capabilities")).json()
        assert capabilities["items"] == [] and capabilities["total"] == 0
        assert (await web.get(f"/api/v1/mcps/{row.id}/capabilities/tools/first")).status_code == 404
        scope = await catalog.index_scope(row, actor["id"])
        assert (await catalog.index.get_summaries([scope]))[row.id]["cache_error"]
        # Failed generations are disposable and the governor may already
        # reclaim them. The primary control owns durable failure diagnostics.
        async with app.state.db.session() as session:
            control = await session.get(CatalogControl, scope_key(scope))
            assert control.health["startup_failure_count"] == 3
            assert control.health["invalidated"]
            assert reason in control.health["last_startup_error"]
        call = await agent(app, web)
        starts = peer.starts
        result = await call("gateway_search_mcps", {"query": "*"})
        service = result.structured_content["items"][0]
        assert service["tools_list"] == [] and service["tool_count"] == 0
        assert service["catalog_status"] == "error"
        assert reason in service["failure_reason"]
        assert service["cache_error_code"]
        assert "administrator" in service["recovery"].lower()
        assert (await call("gateway_search_tools", {"mcp": row.id})).structured_content["tools"] == []
        assert peer.starts == starts, "Read-only discovery must never start downstream"
        peer.failure = peer.discovery_failure = None
        result = await call("gateway_call", {"name": "contract__first", "arguments": {"value": "recover"}})
        assert not result.is_error, json.dumps(result.model_dump(mode="json"), ensure_ascii=True)
        assert (await catalog.get(row.id)).mode == mode


@pytest.mark.parametrize("mode", ["lazy", "eager"])
async def test_failed_runtime_is_observation_not_spec_admission(tmp_path, mode):
    async with console(tmp_path) as (app, _web, _actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({
            "name": "observed", "mode": mode, "config": {"command": "fake"},
        })
        await app.state.runtime.stop_server(row.id, force=True)
        cache = app.state.catalog.cached(row)
        cache.update(runtime_status="failed", startup_failure_count=3, last_startup_error="old outage")
        app.state.catalog.save_cache(row, cache)
        spec = await app.state.catalog.spec(row)
        assert spec.mode == mode
        lease = app.state.runtime.create_lease("system", "business")
        try:
            result = await app.state.catalog.call(row, lease, "first", {"value": 1})
            assert result["content"][0]["text"] == "ok"
        finally:
            await app.state.runtime.release(lease.id)


async def test_failure_reason_is_persisted_without_legacy_json_cache(tmp_path):
    async with console(tmp_path) as (app, web, _actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({
            "name": "persistent", "slug": "persistent", "config": {"command": "fake"},
        })
        peer.failure = OSError("persistent refresh diagnostic")
        with pytest.raises(GatewayError):
            await app.state.catalog.refresh_index(row.id)
        app.state.catalog.delete_cache(row)
        item = (await web.get("/api/v1/mcps")).json()["items"][0]
        assert item["tool_count"] == 1 and item["cache_status"] == "ready"
        assert item["cache_stale"]
        assert item["cache_error"] == "persistent refresh diagnostic"
        call = await agent(app, web)
        result = await call("gateway_search_mcps", {"query": "*"})
        service = result.structured_content["items"][0]
        assert service["tool_count"] == 1
        assert service["cache_stale"]
        assert service["cache_error"] == "persistent refresh diagnostic"


async def test_late_refresh_failure_cannot_clear_newer_success(tmp_path, monkeypatch):
    import asyncio

    async with console(tmp_path) as (app, web, _actor):
        peer = Upstream()
        catalog, runtime = app.state.catalog, app.state.runtime
        runtime.connector = peer.connect
        row = await catalog.create({"name": "race", "config": {"command": "fake"}})
        entered, release = asyncio.Event(), asyncio.Event()
        original = runtime.perform
        calls = 0

        async def delayed(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                raise GatewayError("connection_error", "obsolete failure")
            return await original(*args, **kwargs)

        monkeypatch.setattr(runtime, "perform", delayed)
        pending = asyncio.create_task(catalog.refresh_index(row.id))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await catalog.refresh_index(row.id)
            release.set()
            with pytest.raises(GatewayError):
                await pending
            item = (await web.get("/api/v1/mcps")).json()["items"][0]
            assert item["tool_count"] == 1 and item["cache_status"] == "ready"
            assert item["cache_error"] is None
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_personal_refresh_failure_does_not_invalidate_other_owner(tmp_path):
    async with console(tmp_path) as (app, _web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        catalog = app.state.catalog
        row = await catalog.create({
            "name": "personal", "isolation": "user", "config": {"command": "fake"},
        }, user_id=actor["id"])
        await catalog.refresh_index(row.id, "other")
        peer.discovery_failure = RuntimeError("owner-only failure")
        with pytest.raises(GatewayError):
            await catalog.refresh_index(row.id, actor["id"])
        scopes = [await catalog.index_scope(row, owner) for owner in (actor["id"], "other")]
        own = await catalog.index.list_capabilities(scopes[0], kind="tools")
        other = await catalog.index.list_capabilities(scopes[1], kind="tools")
        assert [item["name"] for item in own.items] == ["first"]
        assert catalog.cached(row, actor["id"])["startup_failure_count"] == 1
        assert catalog.cached(row, "other")["startup_failure_count"] == 0
        assert [item["name"] for item in other.items] == ["first"]
