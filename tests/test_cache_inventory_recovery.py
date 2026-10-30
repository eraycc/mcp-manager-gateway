"""Losing the disposable DB must preserve durable and manifest-pinned bodies."""
from types import SimpleNamespace

from sqlalchemy import func, select

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.catalog_models import CatalogEntry
from mcp_manager.catalog_store import CatalogStore
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.database import Database, McpServer


async def test_cache_inventory_rebuild_does_not_publish_unverified_historical_head(db_settings):
    from mcp_manager.manifest_inventory import recover_cache_inventory
    db = Database(db_settings)
    await db.initialize()
    artifacts = ArtifactStore(db_settings.data_dir, gc_grace_seconds=0)
    async with db.locked() as session:
        row = McpServer(name="Inventory", slug="inventory", transport="stdio", config={})
        session.add(row)
        await session.flush()
    scope = CatalogScope(row.id, "service", 1, "shared")
    store = CatalogStore(db, artifacts, signing_key="test")
    generation = await store.begin_refresh(scope)
    await store.stage_page(generation.id, "tools", [{"name": "keep", "inputSchema": {}}])
    await store.publish(generation.id, expected_scope=scope)
    ref = await store.get_tool(scope, "keep")
    object_path = artifacts._path(ref.artifact_id)
    artifacts.close()
    await db.close()
    (db_settings.data_dir / "indexes/derived.sqlite").unlink()
    db = Database(db_settings)
    await db.initialize()
    artifacts = ArtifactStore(db_settings.data_dir, gc_grace_seconds=0)
    try:
        state = SimpleNamespace(db=db, catalog=SimpleNamespace(artifacts=artifacts))
        for _ in range(20):
            if await recover_cache_inventory(state, limit=5):
                break
        assert artifacts.inventory.roots_ready("cache")
        assert object_path.is_file()
        async with db.derived.session() as session:
            assert await session.scalar(select(func.count()).select_from(CatalogEntry)) == 0
        restored = CatalogStore(db, artifacts, signing_key="test")
        assert (await restored.list_capabilities(scope, kind="tools")).items == []
    finally:
        artifacts.close()
        await db.close()
