"""Control receipts fence cache publication independently of derived transactions."""
import pytest

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.catalog_control import CatalogControl
from mcp_manager.catalog_models import CatalogHead
from mcp_manager.catalog_store import CatalogStore, scope_key
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.database import McpServer


async def setup_store(db, settings):
    async with db.locked() as session:
        row = McpServer(name="Control", slug="control", transport="stdio", config={})
        session.add(row)
        await session.flush()
    scope = CatalogScope(row.id, "service", 1, "shared")
    store = CatalogStore(db, ArtifactStore(settings.data_dir), signing_key="test")
    return store, scope


async def staged(store, scope, name):
    generation = await store.begin_refresh(scope)
    await store.stage_page(generation.id, "tools", [{"name": name, "inputSchema": {}}])
    return generation


async def test_publication_uses_authoritative_fence_without_main_head(dual_db, db_settings):
    db, settings = dual_db, db_settings
    store, scope = await setup_store(db, settings)
    old = await staged(store, scope, "old")
    newer = await staged(store, scope, "new")
    async with db.session() as session:
        control = await session.get(CatalogControl, scope_key(scope))
        assert control is not None and control.refresh_fence == 2
        assert "generation" not in str(control.receipt)
    with pytest.raises(Exception, match="superseded"):
        await store.publish(old.id, expected_scope=scope)
    await store.publish(newer.id, expected_scope=scope)
    assert (await store.list_capabilities(scope, kind="tools")).items[0]["name"] == "new"


async def test_committed_receipt_recovers_missing_materialization_without_reapplying_success(dual_db, db_settings, monkeypatch):
    db, settings = dual_db, db_settings
    store, scope = await setup_store(db, settings)
    generation = await staged(store, scope, "recover")
    original = store._materialize
    async def crash(*args, **kwargs):
        raise RuntimeError("crash after primary receipt")
    monkeypatch.setattr(store, "_materialize", crash)
    with pytest.raises(RuntimeError, match="crash"):
        await store.publish(generation.id, expected_scope=scope)
    async with db.session() as session:
        control = await session.get(CatalogControl, scope_key(scope))
        assert control.receipt["refresh"]["state"] == "committed"
        sequence = control.success_sequence
    monkeypatch.setattr(store, "_materialize", original)
    assert (await store.get_summary(scope))["generation"] == generation.id
    async with db.session() as session:
        control = await session.get(CatalogControl, scope_key(scope))
        assert control.success_sequence == sequence
    async with db.derived.session() as session:
        assert (await session.get(CatalogHead, scope_key(scope))).pending_id is None
