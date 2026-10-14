"""Cache lifetime follows MCP changes, not clocks or size limits."""
import importlib

import pytest
from sqlalchemy import select
from test_catalog_paged_api import indexed_catalog
from test_embedding_scale import CONFIG

from mcp_manager.catalog_models import CatalogEntry
from mcp_manager.database import McpServer, now
from mcp_manager.embedding_models import EmbeddingVector
from mcp_manager.retention import run_storage_retention


@pytest.fixture
async def indexed_vectors(db_app, monkeypatch):
    state = db_app.app.state
    state.retention_next = float("inf")
    row, tools = await indexed_catalog(db_app, 3)
    scope = await state.catalog.index_scope(row)
    generation = (await state.catalog.index.get_summary(scope))["generation"]
    requests = []

    async def embed(endpoint, model, api_key, timeout, inputs, **kwargs):
        requests.extend(inputs)
        return [[1.0, 1.0] for _ in inputs]

    monkeypatch.setattr(state.embeddings, "_embed", embed)
    await db_app.web.patch("/api/v1/settings/embedding", json=CONFIG)
    await state.embeddings.build_generation_vectors(scope, generation, CONFIG)
    requests.clear()
    return row, scope, tools, requests


async def publish(state, scope, tools):
    generation = await state.catalog.index.begin_refresh(scope)
    await state.catalog.index.stage_page(generation.id, "tools", tools)
    await state.catalog.index.publish(generation.id, expected_scope=scope)
    return generation.id


async def test_identical_refresh_reuses_vectors_without_model_requests(db_app, indexed_vectors):
    _row, scope, tools, requests = indexed_vectors
    state = db_app.app.state
    generation = await publish(state, scope, tools)
    result = await state.embeddings.build_generation_vectors(scope, generation, CONFIG)
    assert result["built"] == 0 and requests == []
    async with state.db.session() as session:
        vectors = list((await session.scalars(select(EmbeddingVector))).all())
    assert len(vectors) == 3 and all(v.generation_id == generation for v in vectors)


async def test_delta_refresh_builds_only_new_or_changed_tools_and_drops_removed(db_app, indexed_vectors):
    _row, scope, tools, requests = indexed_vectors
    state = db_app.app.state
    changed = dict(tools[1], inputSchema={"type": "object", "properties": {"changed": {"type": "string"}}})
    added = {"name": "added", "inputSchema": {"type": "object"}}
    generation = await publish(state, scope, [tools[2], changed, added])
    result = await state.embeddings.build_generation_vectors(scope, generation, CONFIG)
    assert result["built"] == 2 and len(requests) == 2
    async with state.db.session() as session:
        names = list((await session.scalars(select(CatalogEntry.original_name).join(
            EmbeddingVector, EmbeddingVector.entry_id == CatalogEntry.id
        ))).all())
    assert set(names) == {tools[2]["name"], changed["name"], "added"}


async def test_config_revision_change_preserves_identical_tool_vectors(db_app, indexed_vectors):
    row, _scope, tools, requests = indexed_vectors
    state = db_app.app.state
    async with state.db.locked() as session:
        (await session.get(McpServer, row.id)).revision += 1
    fresh = await state.catalog.get(row.id)
    new_scope = await state.catalog.index_scope(fresh)
    generation = await publish(state, new_scope, tools)
    assert (await state.embeddings.build_generation_vectors(new_scope, generation, CONFIG))["built"] == 0
    assert requests == []


async def test_runtime_delete_removes_only_target_catalog_and_embedding_cache(db_app, indexed_vectors):
    row, _scope, _tools, _requests = indexed_vectors
    state = db_app.app.state
    other, _ = await indexed_catalog(db_app, 0)
    target_key = state.catalog.cache_key(row)
    other_key = state.catalog.cache_key(other)
    state.catalog.cache_store.set(target_key, {"revision": row.revision, "data": {}})
    state.catalog.cache_store.set(other_key, {"revision": other.revision, "data": {}})
    metadata = {"cache_version": 2, "model_hash": "same", "vector": [1.0, 1.0]}
    state.embeddings.store.set("target", dict(metadata, references=[[row.id, row.revision]]))
    state.embeddings.store.set("other", dict(metadata, references=[[other.id, other.revision]]))
    await state.catalog.delete(row.id)
    assert state.catalog.cache_store.get(target_key) is None
    assert state.catalog.cache_store.get(other_key) is not None
    assert state.embeddings.store.get("target") is None
    assert state.embeddings.store.get("other") is not None
    assert row.id not in state.catalog.cache_store.path.read_text()
    assert "target" not in state.embeddings.store.path.read_text()
    async with state.db.session() as session:
        assert list((await session.scalars(select(EmbeddingVector))).all()) == []


async def test_hourly_retention_does_not_rewrite_or_prune_cache(db_app, indexed_vectors, monkeypatch):
    state = db_app.app.state

    def forbidden(*args, **kwargs):
        raise AssertionError("Hourly retention must not touch catalog or embedding cache")

    monkeypatch.setattr(state.catalog.cache_store, "compact", forbidden)
    monkeypatch.setattr(state.embeddings.store, "delete_keys", forbidden)
    await run_storage_retention(state, at=now())


async def test_startup_cleanup_runs_once_not_once_per_hour(db_app, indexed_vectors, monkeypatch):
    module = importlib.import_module("mcp_manager.app")
    from mcp_manager.retention import run_startup_cache_cleanup

    calls = []

    async def counted(state):
        calls.append(True)
        await run_startup_cache_cleanup(state)

    monkeypatch.setattr("mcp_manager.retention.run_startup_cache_cleanup", counted)
    state = db_app.app.state
    state.cache_cleanup_done = False
    state.retention_next = 0
    await module._log_retention(db_app.app)
    state.retention_next = 0
    await module._log_retention(db_app.app)
    assert calls == [True]


async def test_disable_does_not_trigger_cache_cleanup_or_drop_vectors(db_app, indexed_vectors, monkeypatch):
    row, _scope, _tools, _requests = indexed_vectors
    state = db_app.app.state

    async def forbidden(*args, **kwargs):
        raise AssertionError("Disabling is not a cache invalidation event")

    monkeypatch.setattr(state.catalog, "on_cache_change", forbidden)
    monkeypatch.setattr(state.catalog.index, "on_publish", forbidden)
    await state.catalog.update(row.id, {"mode": "disabled"}, user_id=db_app.actor["id"])
    async with state.db.session() as session:
        vectors = list((await session.scalars(select(EmbeddingVector))).all())
    assert len(vectors) == 3


async def test_legacy_resource_cache_follows_exact_definition_changes(db_app, indexed_vectors):
    import hashlib
    import json

    from mcp_manager.embeddings import resource_vector_hash

    row, scope, tools, _requests = indexed_vectors
    state = db_app.app.state
    resources = [{"name": name, "uri": f"test://{name}", "description": "original"} for name in ("stable", "changed", "removed")]
    generation = await state.catalog.index.begin_refresh(scope)
    await state.catalog.index.stage_page(generation.id, "tools", tools)
    await state.catalog.index.stage_page(generation.id, "resources", resources)
    await state.catalog.index.publish(generation.id, expected_scope=scope)
    docs = [{"id": item["name"], "text": json.dumps({key: item[key] for key in ("name", "description")}),
             "cache_ref": [row.id, row.revision],
             "cache_capability": [row.id, "service", "resources", hashlib.sha256(item["uri"].encode()).hexdigest()]}
            for item in resources]
    await state.embeddings.scores("resource-scope", docs, "query", CONFIG)
    original = list(state.embeddings.store.items.values())
    # Obsolete legacy arrays must not resurrect a resource missing from a published head.
    state.catalog.save_cache(row, {"resources": resources, "cache_status": "ready"})
    stable_hash = resource_vector_hash(resources[0])
    generation = await state.catalog.index.begin_refresh(scope)
    await state.catalog.index.stage_page(generation.id, "tools", tools)
    await state.catalog.index.stage_page(generation.id, "resources", [
        resources[0], dict(resources[1], description="changed description"),
    ])
    await state.catalog.index.publish(generation.id, expected_scope=scope)
    cached_documents = [value for value in state.embeddings.store.items.values() if value.get("capability")]
    assert len(cached_documents) == 1 and cached_documents[0]["content_hash"] == stable_hash
    assert cached_documents[0] in original


@pytest.mark.parametrize("cache", ["catalog", "embedding"])
async def test_deleted_cache_rewrite_failure_retries_without_hourly_cleanup(db_app, indexed_vectors, monkeypatch, cache):
    from mcp_manager.retention import run_startup_cache_cleanup

    row, _scope, _tools, _requests = indexed_vectors
    state = db_app.app.state
    state.cache_cleanup_done = True
    state.catalog.cache_store.set(state.catalog.cache_key(row), {"revision": row.revision, "data": {}})
    state.embeddings.store.set("target", {
        "vector": [1.0, 1.0], "cache_version": 2,
        "references": [[row.id, row.revision]], "model_hash": "same",
    })

    def blocked(keys):
        raise OSError("locked")

    with monkeypatch.context() as patch:
        store = state.embeddings.store if cache == "embedding" else state.catalog.cache_store
        patch.setattr(store, "delete_keys", blocked)
        await state.catalog.delete(row.id)
    assert state.cache_cleanup_done is False and state.retention_next == 0
    await run_startup_cache_cleanup(state)
    assert state.embeddings.store.get("target") is None
    assert row.id not in state.catalog.cache_store.path.read_text()
