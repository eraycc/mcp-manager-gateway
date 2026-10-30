"""History and collection have fixed budgets and preserve true domain roots."""
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.catalog_models import CatalogEntry
from mcp_manager.catalog_store import CatalogStore
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.database import McpServer, now
from mcp_manager.job_store import JobStore


async def test_historical_manifests_have_two_item_limit_without_historical_postings(dual_db, db_settings):
    from mcp_manager import catalog_models
    assert hasattr(catalog_models, "CatalogManifest"), "Need bounded manifest history"
    from mcp_manager.retention import _prune_capability_rows
    async with dual_db.locked() as session:
        row = McpServer(name="History", slug="history", transport="stdio", config={})
        session.add(row)
        await session.flush()
    scope = CatalogScope(row.id, "service", 1, "shared")
    store = CatalogStore(dual_db, ArtifactStore(db_settings.data_dir), signing_key="test")
    for version in range(6):
        generation = await store.begin_refresh(scope)
        await store.stage_page(generation.id, "tools", [{"name": f"tool{version}", "inputSchema": {}}])
        await store.publish(generation.id, expected_scope=scope)
    state = SimpleNamespace(db=dual_db)
    for _ in range(20):
        await _prune_capability_rows(state, now(), 10)
    async with dual_db.derived.session() as session:
        manifests = (await session.scalars(select(catalog_models.CatalogManifest))).all()
        assert len(manifests) == 3
        assert sum(item.current for item in manifests) == 1
        assert await session.scalar(select(func.count()).select_from(CatalogEntry)) == 1
    assert (await store.list_capabilities(scope, kind="tools")).items[0]["name"] == "tool5"
    for _ in range(20):
        await _prune_capability_rows(state, now() + timedelta(hours=25), 10)
    async with dual_db.derived.session() as session:
        assert await session.scalar(select(func.count()).select_from(catalog_models.CatalogManifest)) == 1


async def test_collector_preserves_persistent_main_roots_when_cache_registry_is_empty(dual_db, db_settings):
    from mcp_manager.retention import collect_objects
    cache = ArtifactStore(db_settings.data_dir, gc_grace_seconds=0)
    jobs = JobStore(dual_db, cache)
    job = await jobs.create_job("evidence", 1)
    await jobs.append_result(job["id"], 0, {"ok": True, "result": {"value": "keep"}})
    durable = jobs.artifacts
    durable.gc_grace_seconds = 0
    orphan = await durable.put_json({"orphan": True}, max_bytes=1024)
    state = SimpleNamespace(db=dual_db, catalog=SimpleNamespace(artifacts=cache), jobs=SimpleNamespace(store=jobs))
    for _ in range(3):
        await collect_objects(state, namespace="durable", limit=10)
    assert not durable._path(orphan.id).exists()
    assert (await jobs.get_result(job["id"], 0))["result"]["value"] == "keep"
