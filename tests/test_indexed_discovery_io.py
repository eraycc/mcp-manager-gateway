"""Indexed searches never read per-provider CAS compatibility snapshots."""
import pytest
from test_catalog_paged_api import indexed_catalog
from test_exact_route_scalability import gateway_context, invoke


@pytest.mark.parametrize("name,arguments", [
    ("gateway_search_mcps", {"query": "*"}),
    ("gateway_search_tools", {"mcp": "*", "limit": 2}),
])
async def test_indexed_discovery_uses_batched_metadata_without_compatibility_reads(db_app, monkeypatch, name, arguments):
    await indexed_catalog(db_app, 3)
    context, _ = await gateway_context(db_app)
    def forbidden(*args, **kwargs):
        raise AssertionError("Indexed search read a per-provider compatibility snapshot")
    monkeypatch.setattr(db_app.app.state.catalog, "cached_view", forbidden)
    result = await invoke(db_app, context, name, arguments)
    assert not result.is_error, result
    assert result.structured_content["returned"] > 0


async def test_repeated_indexed_search_reuses_summary_but_rechecks_current_authorization(db_app, monkeypatch):
    await indexed_catalog(db_app, 3)
    context, _ = await gateway_context(db_app)
    result = await invoke(db_app, context, "gateway_search_tools", {"mcp": "*", "limit": 2})
    assert not result.is_error
    original_principal = db_app.app.state.gateway.principal
    checks = []

    async def principal(*args, **kwargs):
        checks.append(True)
        return await original_principal(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unchanged indexed search rebuilt all provider summaries")

    monkeypatch.setattr(db_app.app.state.gateway, "principal", principal)
    monkeypatch.setattr(db_app.app.state.catalog, "index_scopes", forbidden)
    result = await invoke(db_app, context, "gateway_search_tools", {"mcp": "*", "limit": 2})
    assert not result.is_error
    assert len(checks) >= 2, "Cache hits must still revalidate current permissions"


async def test_concurrent_cold_searches_share_one_summary_build(db_app, monkeypatch):
    import asyncio

    await indexed_catalog(db_app, 3)
    context, _ = await gateway_context(db_app)
    catalog = db_app.app.state.catalog
    original = catalog.index_scopes
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def delayed(*args, **kwargs):
        calls.append(True)
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(catalog, "index_scopes", delayed)
    first = asyncio.create_task(invoke(db_app, context, "gateway_search_tools", {"mcp": "*", "limit": 2}))
    await asyncio.wait_for(entered.wait(), 3)
    second = asyncio.create_task(invoke(db_app, context, "gateway_search_tools", {"mcp": "*", "limit": 2}))
    try:
        await asyncio.sleep(.1)
        assert len(calls) == 1, "Concurrent cold requests duplicated the full directory build"
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(not result.is_error for result in results)
