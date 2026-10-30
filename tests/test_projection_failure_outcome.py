"""Disposable projection failures must never mask an already completed business call."""
import pytest
from test_catalog_paged_api import indexed_catalog
from test_exact_route_scalability import downstream, gateway_context, invoke

from mcp_manager.catalog import alias
from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("code", ["cache_capacity", "staging_capacity", "storage_low_space"])
async def test_completed_call_keeps_its_result_when_compatibility_cache_is_full(db_app, monkeypatch, code):
    state = db_app.app.state
    row, tools = await indexed_catalog(db_app, 1)
    calls = downstream(monkeypatch, db_app, tools)
    await state.catalog.warm(row.id)
    context, _ = await gateway_context(db_app)
    await state.catalog.record_start_failure(row, GatewayError("connection_error", "Injected recoverable failure"))
    calls.clear()
    attempts = []
    def unavailable(key, value, **kwargs):
        if value is not None:
            attempts.append(key)
            raise GatewayError(code, "Injected local disposable projection pressure")
    monkeypatch.setattr(state.catalog.cache_store, "set", unavailable)
    result = await invoke(db_app, context, "gateway_call", {
        "name": alias(row.slug, tools[0]["name"]), "arguments": {}})
    assert attempts, "The disposable projection fault was never exercised"
    assert calls == [(tools[0]["name"], {})], "Completed business operation was replayed"
    assert not result.is_error, result
    assert (await state.logs.query())["items"][0]["status"] == "success"
