"""Catalog publication must be complete, generation-bound and owner-isolated."""
import importlib.util

import pytest

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.database import McpServer
from mcp_manager.runtime import GatewayError


async def catalog_store(dual_db, db_settings):
    assert importlib.util.find_spec("mcp_manager.catalog_store"), "Full directories need indexed generation storage"
    from mcp_manager.catalog_store import CatalogStore
    async with dual_db.locked() as session:
        row = McpServer(name="Large service", slug="large", transport="stdio", config={}, isolation="user")
        session.add(row)
        await session.flush()
    artifacts = ArtifactStore(db_settings.data_dir)
    return CatalogStore(dual_db, artifacts, signing_key=db_settings.jwt_secret), artifacts, row


async def publish(store, scope, names):
    generation = await store.begin_refresh(scope)
    await store.stage_page(generation.id, "tools", [
        {"name": name, "description": "Useful " + name, "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string"}
        }}} for name in names
    ])
    return await store.publish(generation.id, expected_scope=scope)


async def test_reader_sees_only_complete_published_generation(dual_db, db_settings):
    store, _artifacts, row = await catalog_store(dual_db, db_settings)
    scope = CatalogScope(row.id, "alice", row.revision, "credential-v1")
    original = await publish(store, scope, ["old"])
    staging = await store.begin_refresh(scope)
    await store.stage_page(staging.id, "tools", [{"name": "new-a", "inputSchema": {"type": "object"}}])
    before = await store.list_capabilities(scope, kind="tools", query="", cursor=None, limit=50)
    assert [item["name"] for item in before.items] == ["old"]
    assert before.generation == original.id
    await store.stage_page(staging.id, "tools", [{"name": "new-b", "inputSchema": {"type": "object"}}])
    await store.publish(staging.id, expected_scope=scope)
    after = await store.list_capabilities(scope, kind="tools", query="", cursor=None, limit=50)
    assert [item["name"] for item in after.items] == ["new-a", "new-b"]
    assert after.generation == staging.id


async def test_owner_isolation_and_summary_never_reads_schema(dual_db, db_settings, monkeypatch):
    store, artifacts, row = await catalog_store(dual_db, db_settings)
    alice = CatalogScope(row.id, "alice", row.revision, "alice-credentials")
    bob = CatalogScope(row.id, "bob", row.revision, "bob-credentials")
    await publish(store, alice, ["private-alice"])
    await publish(store, bob, ["private-bob"])
    async def forbidden_read(*args, **kwargs):
        raise AssertionError("Summary operations must not read Schema objects")
    monkeypatch.setattr(artifacts, "read_json", forbidden_read)
    summary = await store.get_summary(alice)
    page = await store.list_capabilities(bob, kind="tools", query="", cursor=None, limit=50)
    assert summary["tool_count"] == 1
    assert [item["name"] for item in page.items] == ["private-bob"]
    assert "inputSchema" not in page.items[0]


async def test_stale_configuration_or_credential_scope_cannot_publish(dual_db, db_settings):
    store, _artifacts, row = await catalog_store(dual_db, db_settings)
    scope = CatalogScope(row.id, "alice", row.revision, "credential-v1")
    staged = await store.begin_refresh(scope)
    await store.stage_page(staged.id, "tools", [{"name": "old", "inputSchema": {"type": "object"}}])
    with pytest.raises(GatewayError):
        await store.publish(staged.id, expected_scope=CatalogScope(row.id, "alice", row.revision, "changed"))
    async with dual_db.locked() as session:
        (await session.get(McpServer, row.id)).revision += 1
    with pytest.raises(GatewayError) as caught:
        await store.publish(staged.id, expected_scope=scope)
    assert caught.value.code == "revision_changed"
    assert (await store.get_summary(scope))["tool_count"] == 0


async def test_newer_refresh_prevents_older_publish(dual_db, db_settings):
    store, _artifacts, row = await catalog_store(dual_db, db_settings)
    scope = CatalogScope(row.id, "alice", row.revision, "credential-v1")
    old = await store.begin_refresh(scope)
    await store.stage_page(old.id, "tools", [{"name": "old", "inputSchema": {"type": "object"}}])
    newer = await publish(store, scope, ["new"])
    with pytest.raises(GatewayError) as caught:
        await store.publish(old.id, expected_scope=scope)
    assert caught.value.code == "generation_superseded"
    assert (await store.get_summary(scope))["generation"] == newer.id


async def test_staging_batch_size_is_bounded(dual_db, db_settings):
    store, _artifacts, row = await catalog_store(dual_db, db_settings)
    scope = CatalogScope(row.id, "alice", row.revision, "v1")
    staged = await store.begin_refresh(scope)
    with pytest.raises(ValueError):
        await store.stage_page(staged.id, "tools", [{"name": str(index)} for index in range(251)])
    assert (await store.get_summary(scope))["tool_count"] == 0


async def test_exact_route_and_schema_reference_preserve_original_name(dual_db, db_settings):
    store, _artifacts, row = await catalog_store(dual_db, db_settings)
    scope = CatalogScope(row.id, "alice", row.revision, "v1")
    await publish(store, scope, ["Read", "read", "读取😀"])
    first = await store.get_tool(scope, "Read")
    second = await store.get_tool(scope, "read")
    assert first is not None and second is not None and first.artifact_id != second.artifact_id
    assert first.gateway_name == "large__Read"
    assert await store.resolve_gateway_name(first.gateway_name, {row.id}, owner="bob") is None
    exact = await store.resolve_gateway_name(first.gateway_name, {row.id}, owner="alice")
    assert exact == first
    assert await store.resolve_gateway_name(first.gateway_name, set(), owner="alice") is None
    definition = await store.get_definition(scope, kind="tools", name="读取😀")
    assert definition["inputSchema"] == {"type": "object", "properties": {"path": {"type": "string"}}}


async def test_filtered_pagination_is_complete_and_cursor_rejects_query_change(dual_db, db_settings):
    store, _artifacts, row = await catalog_store(dual_db, db_settings)
    scope = CatalogScope(row.id, "alice", row.revision, "v1")
    await publish(store, scope, ["match-01", "other", "match-02", "match-03"])
    page = await store.list_capabilities(scope, kind="tools", query="match", cursor=None, limit=2)
    assert page.total == 3 and page.next_cursor
    assert [item["name"] for item in page.items] == ["match-01", "match-02"]
    last = await store.list_capabilities(scope, kind="tools", query="match", cursor=page.next_cursor, limit=2)
    assert [item["name"] for item in last.items] == ["match-03"] and last.next_cursor is None
    with pytest.raises(GatewayError):
        await store.list_capabilities(scope, kind="tools", query="other", cursor=page.next_cursor, limit=2)
    with pytest.raises(GatewayError):
        await store.list_capabilities(CatalogScope(row.id, "bob", row.revision, "v1"),
                                      kind="tools", query="match", cursor=page.next_cursor, limit=2)
