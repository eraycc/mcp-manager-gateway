"""Physical cache retention must preserve logical catalogs, disabled and failed scopes."""
import asyncio
import hashlib

import pytest
from sqlalchemy import select
from test_catalog_paged_api import indexed_catalog
from test_embedding_scale import CONFIG

from mcp_manager.catalog_models import CatalogEntry, CatalogHead
from mcp_manager.database import McpServer, now
from mcp_manager.embedding_models import EmbeddingVector
from mcp_manager.embeddings import _settings
from mcp_manager.retention import run_storage_retention


@pytest.fixture
async def vector_catalog(db_app, monkeypatch):
    state = db_app.app.state
    state.retention_next = float("inf")
    row, _ = await indexed_catalog(db_app, 1)
    scope = await state.catalog.index_scope(row)
    summary = await state.catalog.index.get_summary(scope)

    async def fake_embed(endpoint, model, api_key, timeout, inputs, **kwargs):
        return [[1.0, 1.0] for _ in inputs]

    monkeypatch.setattr(state.embeddings, "_embed", fake_embed)
    await db_app.web.patch("/api/v1/settings/embedding", json=CONFIG)
    await state.embeddings.build_generation_vectors(scope, summary["generation"], CONFIG)
    return row, scope, summary["generation"]


async def vector_rows(state):
    async with state.db.session() as session:
        return list((await session.scalars(select(EmbeddingVector))).all())


async def test_retention_removes_old_models_even_when_generation_is_current(db_app, vector_catalog):
    _row, scope, generation = vector_catalog
    state = db_app.app.state
    changed = dict(CONFIG, model="new-model")
    await db_app.web.patch("/api/v1/settings/embedding", json=changed)
    await state.embeddings.build_generation_vectors(scope, generation, changed)
    assert len(await vector_rows(state)) == 2
    result = await run_storage_retention(state, at=now())
    assert result["by_kind"]["embedding_vectors"] == 1
    rows = await vector_rows(state)
    endpoint, model, *_ = _settings(changed)
    assert len(rows) == 1 and rows[0].model_hash == state.embeddings._model_hash(endpoint, model)
    async with state.db.session() as session:
        entry = await session.get(CatalogEntry, rows[0].entry_id)
    assert rows[0].content_hash == hashlib.sha256(entry.search_text.encode()).hexdigest()


@pytest.mark.parametrize("protected", ["disabled", "failed"])
async def test_retention_keeps_vectors_for_disabled_and_failed_scopes(db_app, vector_catalog, protected):
    row, _scope, generation = vector_catalog
    state = db_app.app.state
    async with state.db.locked() as session:
        if protected == "disabled":
            (await session.get(McpServer, row.id)).mode = "disabled"
        else:
            head = await session.scalar(select(CatalogHead).where(CatalogHead.generation_id == generation))
            head.status = "error"
    await db_app.web.patch("/api/v1/settings/embedding", json=dict(CONFIG, model="new-model"))
    before = [(v.generation_id, v.entry_id, v.model_hash) for v in await vector_rows(state)]
    await run_storage_retention(state, at=now())
    assert [(v.generation_id, v.entry_id, v.model_hash) for v in await vector_rows(state)] == before


async def test_retention_removes_unreferenced_generation_vectors_in_bounded_batches(db_app, vector_catalog):
    _row, scope, _previous = vector_catalog
    state = db_app.app.state
    fresh = await state.catalog.index.begin_refresh(scope)
    await state.catalog.index.stage_page(fresh.id, "tools", [{"name": "new", "inputSchema": {"type": "object"}}])
    await state.catalog.index.publish(fresh.id, expected_scope=scope)
    result = await run_storage_retention(state, at=now(), batch_limit=1)
    assert result["deleted"] <= 1
    assert result["by_kind"]["embedding_vectors"] == 1
    assert await vector_rows(state) == []


async def test_legacy_vectors_get_reclaimable_metadata_without_changing_hashes(db_app, vector_catalog):
    row, _, _ = vector_catalog
    state = db_app.app.state
    docs = [{"id": "doc", "text": "weather", "cache_ref": [row.id, row.revision]}]
    scores, _ = await state.embeddings.scores("scope", docs, "forecast", CONFIG)
    assert scores
    endpoint, model, *_ = _settings(CONFIG)
    content_hash = hashlib.sha256(b"weather").hexdigest()
    key = state.embeddings._cache_key(endpoint, model, "scope", "doc", content_hash)
    value = state.embeddings.store.get(key)
    assert value["vector"] == [1.0, 1.0]
    assert value["cache_version"] == 1
    assert value["references"] == [[row.id, row.revision]]
    assert value["model_hash"] == state.embeddings._model_hash(endpoint, model)


async def test_retention_rebuilds_opaque_legacy_cache_and_compacts_catalog(db_app, vector_catalog):
    state = db_app.app.state
    store = state.embeddings.store
    store.set("opaque-legacy", {"vector": [1.0, 1.0]})
    catalog = state.catalog.cache_store
    catalog.set("keep", {"revision": 1, "data": {}})
    catalog.set("gone", {"payload": "x" * 4096})
    catalog.set("gone", None)
    await run_storage_retention(state, at=now())
    assert store.get("opaque-legacy") is None
    assert "opaque-legacy" not in store.path.read_text()
    assert "gone" not in catalog.path.read_text()
    assert catalog.get("keep") is not None


@pytest.mark.parametrize("protected", ["disabled", "failed"])
async def test_scoped_legacy_cache_is_unchanged_for_disabled_or_failed_services(db_app, vector_catalog, protected):
    row, _, generation = vector_catalog
    state = db_app.app.state
    await state.embeddings.scores("scope", [{"id": "doc", "text": "weather", "cache_ref": [row.id, row.revision]}],
                                  "forecast", CONFIG)
    before = state.embeddings.store.path.read_bytes()
    async with state.db.locked() as session:
        if protected == "disabled":
            (await session.get(McpServer, row.id)).mode = "disabled"
        else:
            head = await session.scalar(select(CatalogHead).where(CatalogHead.generation_id == generation))
            head.status = "error"
    await db_app.web.patch("/api/v1/settings/embedding", json=dict(CONFIG, model="changed"))
    await run_storage_retention(state, at=now())
    assert state.embeddings.store.path.read_bytes() == before


async def test_deleted_service_legacy_vectors_are_physically_reclaimed(db_app, vector_catalog):
    row, _, _ = vector_catalog
    state = db_app.app.state
    await state.embeddings.scores("scope", [{"id": "doc", "text": "weather", "cache_ref": [row.id, row.revision]}],
                                  "forecast", CONFIG)
    async with state.db.locked() as session:
        await session.delete(await session.get(McpServer, row.id))
    await run_storage_retention(state, at=now())
    assert state.embeddings.store.items == {}
    assert state.embeddings.store.path.stat().st_size == 0


async def test_retention_file_failure_preserves_snapshots_and_can_retry(db_app, vector_catalog, monkeypatch):
    state = db_app.app.state
    store = state.embeddings.store
    store.set("opaque", {"vector": [1.0, 1.0]})
    before = store.path.read_bytes()

    def blocked(keys):
        raise OSError("locked")

    with monkeypatch.context() as patch:
        patch.setattr(store, "delete_keys", blocked)
        with pytest.raises(OSError):
            await run_storage_retention(state, at=now())
    assert store.path.read_bytes() == before
    await run_storage_retention(state, at=now())
    assert store.items == {}


async def test_cache_rewrite_runs_off_the_event_loop(db_app, vector_catalog, monkeypatch):
    state = db_app.app.state
    loop = asyncio.get_running_loop()
    calls = []
    original = state.catalog.cache_store.compact

    def threaded():
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        calls.append(True)
        return original()

    monkeypatch.setattr(state.catalog.cache_store, "compact", threaded)
    await run_storage_retention(state, at=now())
    assert calls and loop is asyncio.get_running_loop()


async def test_legacy_cache_prunes_expired_and_excess_live_entries(db_app, vector_catalog, monkeypatch):
    import time

    row, _, _ = vector_catalog
    state = db_app.app.state
    endpoint, model, *_ = _settings(CONFIG)
    metadata = {"cache_version": 1, "model_hash": state.embeddings._model_hash(endpoint, model),
                "references": [[row.id, row.revision]], "vector": [1.0, 1.0]}
    store = state.embeddings.store
    stamp = time.time()
    store.set("expired", dict(metadata, cached_at=0))
    for index in range(4):
        store.set(f"live-{index}", dict(metadata, cached_at=stamp + index))
    monkeypatch.setattr("mcp_manager.embeddings.LEGACY_CACHE_MAX_ENTRIES", 2)
    await run_storage_retention(state, at=now())
    assert set(store.items) == {"live-2", "live-3"}
    assert store.records == 2
