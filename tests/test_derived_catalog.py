"""Catalog and durable evidence use distinct storage authorities."""
from sqlalchemy import delete, inspect, select

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.catalog_models import ArtifactObject, CatalogEntry
from mcp_manager.catalog_store import CatalogStore
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.database import McpServer
from mcp_manager.job_store import JobStore

CACHE_TABLES = {
    "artifact_objects", "catalog_generations", "catalog_heads",
    "catalog_entries", "catalog_terms", "embedding_vectors",
}


async def test_new_main_schema_has_no_reconstructible_cache_tables(dual_db):
    async with dual_db.engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
    assert CACHE_TABLES.isdisjoint(tables), tables & CACHE_TABLES
    assert {"users", "mcp_servers", "jobs", "job_results", "proposal_test_entries"} <= tables


async def test_catalog_publish_and_owner_lookup_use_only_derived_index(dual_db, db_settings):
    async with dual_db.locked() as session:
        server = McpServer(name="Derived", slug="derived", transport="stdio", config={}, isolation="user")
        session.add(server)
        await session.flush()
    store = CatalogStore(dual_db, ArtifactStore(db_settings.data_dir), signing_key=db_settings.jwt_secret)
    alice = CatalogScope(server.id, "alice", 1, "grant-alice")
    bob = CatalogScope(server.id, "bob", 1, "grant-bob")
    generation = await store.begin_refresh(alice)
    definition = {"name": "exactRead", "description": "读取 weather records",
                  "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}}
    await store.stage_page(generation.id, "tools", [definition])
    await store.publish(generation.id, expected_scope=alice)
    assert (await store.get_definition(alice, kind="tools", name="exactRead")) == definition
    assert (await store.list_capabilities(bob, kind="tools")).items == []
    async with dual_db.derived.session() as session:
        entries = list((await session.scalars(select(CatalogEntry))).all())
        assert len(entries) == 1 and entries[0].generation_id == generation.id
    async with dual_db.engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
        assert CACHE_TABLES.isdisjoint(tables)
    page = await store.list_capabilities(alice, kind="tools")
    assert page.total == 1 and page.items[0]["gateway_name"] == "derived__exactRead"


async def test_durable_job_result_does_not_depend_on_cache_registry(dual_db, db_settings):
    artifacts = ArtifactStore(db_settings.data_dir)
    jobs = JobStore(dual_db, artifacts)
    job = await jobs.create_job("durable-evidence", 1)
    result = {"item": "source", "ok": True, "result": {"value": "cannot be recomputed"}}
    assert await jobs.append_result(job["id"], 0, result)
    ref = await jobs.get_result_ref(job["id"], 0)
    assert ref.namespace == "durable"
    assert (await jobs.get_result(job["id"], 0))["result"]["value"] == "cannot be recomputed"
    assert list((db_settings.data_dir / "objects/durable").rglob("*.json"))
    async with dual_db.derived.locked() as session:
        await session.execute(delete(ArtifactObject))
    assert (await jobs.get_result(job["id"], 0))["result"]["value"] == "cannot be recomputed"
