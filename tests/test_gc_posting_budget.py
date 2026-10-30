"""Retired posting cleanup gets a real bulk budget independent from live CAS scans."""
from sqlalchemy import func, select
from test_catalog_paged_api import indexed_catalog

from mcp_manager.catalog_models import CatalogTerm
from mcp_manager.database import now


async def test_retired_postings_use_bulk_budget_and_never_remove_current_search(db_app):
    from mcp_manager.retention import drain_retired_postings

    state = db_app.app.state
    await state.storage_governor.close()
    row, tools = await indexed_catalog(db_app, 40)
    scope = await state.catalog.index_scope(row)
    old = (await state.catalog.index.get_summary(scope))["generation"]
    async with state.db.derived.session() as session:
        from mcp_manager.catalog_models import CatalogGeneration
        key = (await session.get(CatalogGeneration, old)).index_id
        before = await session.scalar(select(func.count()).select_from(CatalogTerm).where(CatalogTerm.generation_id == key))
    new = await state.catalog.index.begin_refresh(scope)
    await state.catalog.index.stage_page(new.id, "tools", tools)
    await state.catalog.index.publish(new.id, expected_scope=scope)
    assert before > 500
    removed = await drain_retired_postings(state, at=now(), limit=500)
    assert removed == 500
    async with state.db.derived.session() as session:
        assert await session.scalar(select(func.count()).select_from(CatalogTerm).where(CatalogTerm.generation_id == key)) == before - 500
    assert (await state.catalog.index.get_summary(scope))["generation"] == new.id
    assert (await state.catalog.index.list_capabilities(scope, kind="tools")).total == 40
