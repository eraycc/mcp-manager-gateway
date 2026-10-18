"""A persistence error after publication must not leave a successful catalog visible."""
import pytest
from test_bug1_catalog import console
from test_discovery_failure_policy import Upstream

from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("entry", ["refresh", "refresh_index"])
async def test_cache_write_failure_invalidates_its_own_published_generation(tmp_path, monkeypatch, entry):
    async with console(tmp_path) as (app, web, _actor):
        app.state.runtime.connector = Upstream().connect
        catalog = app.state.catalog
        row = await catalog.create({"name": "write failure", "config": {"command": "fake"}})
        original = catalog.cache_store.set
        failed = False

        def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("cache device temporarily unavailable")
            return original(*args, **kwargs)

        monkeypatch.setattr(catalog.cache_store, "set", fail_once)
        with pytest.raises(GatewayError) as failure:
            await getattr(catalog, entry)(row.id)
        assert failure.value.code == "cache_write_failed"
        item = (await web.get("/api/v1/mcps")).json()["items"][0]
        assert item["cache_status"] == "error" and item["tool_count"] == 0
        assert item["cache_error_code"] == "cache_write_failed"
        assert (await web.get(f"/api/v1/mcps/{row.id}/capabilities")).json()["items"] == []
