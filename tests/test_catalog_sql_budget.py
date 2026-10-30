"""SQL growth is bounded per derived domain while current heads remain searchable."""
import pytest
from sqlalchemy import text
from test_catalog_paged_api import indexed_catalog

from mcp_manager.runtime import GatewayError
from mcp_manager.storage_policy import MIB


async def test_index_quota_keeps_current_head_and_releases_retired_rows(db_app):
    state = db_app.app.state
    row, _ = await indexed_catalog(db_app, 1)
    scope = await state.catalog.index_scope(row)
    current = (await state.catalog.index.get_summary(scope))["generation"]
    saved = await db_app.web.patch("/api/v1/settings/storage/governance", json={
        "catalog_sql_bytes": MIB, "min_free_bytes": 0, "min_free_percent": 0})
    assert saved.status_code == 200
    generation = await state.catalog.index.begin_refresh(scope)
    with pytest.raises(GatewayError) as exc:
        await state.catalog.index.stage_page(generation.id, "tools", [{
            "name": "oversized_index_" + str(index), "description": "x" * 4096,
            "inputSchema": {"type": "object"}} for index in range(200)])
    assert exc.value.code == "index_capacity"
    assert (await state.catalog.index.get_summary(scope))["generation"] == current
    assert (await state.catalog.index.list_capabilities(scope, kind="tools")).items
    async with state.db.derived.session() as session:
        usage = (await session.execute(text(
            "SELECT estimated_bytes,rows_count FROM catalog_sql_usage WHERE name='catalog_entries'"))).one()
        assert usage.estimated_bytes > 0 and usage.rows_count == 1


async def test_sql_meter_follows_rollback_and_cascade(db_app):
    state = db_app.app.state
    row, _ = await indexed_catalog(db_app, 2)
    scope = await state.catalog.index_scope(row)
    generation = (await state.catalog.index.get_summary(scope))["generation"]
    async with state.db.derived.session() as session:
        before = (await session.execute(text("SELECT sum(estimated_bytes),sum(rows_count) FROM catalog_sql_usage"))).one()
    with pytest.raises(RuntimeError, match="rollback"):
        async with state.db.derived.locked() as session:
            await session.execute(text("DELETE FROM catalog_generations WHERE id=:id"), {"id": generation})
            raise RuntimeError("rollback")
    async with state.db.derived.session() as session:
        assert (await session.execute(text("SELECT sum(estimated_bytes),sum(rows_count) FROM catalog_sql_usage"))).one() == before
    async with state.db.derived.locked() as session:
        await session.execute(text("DELETE FROM catalog_generations WHERE id=:id"), {"id": generation})
    async with state.db.derived.session() as session:
        assert (await session.execute(text("SELECT rows_count FROM catalog_sql_usage WHERE name='catalog_entries'"))).scalar_one() == 0
        assert (await session.execute(text("SELECT rows_count FROM catalog_sql_usage WHERE name='catalog_terms'"))).scalar_one() == 0
