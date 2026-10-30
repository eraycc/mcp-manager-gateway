"""Physical cache retention must preserve logical catalogs, disabled and failed scopes."""
import asyncio
import hashlib

import pytest
from sqlalchemy import insert, select
from test_catalog_paged_api import indexed_catalog
from test_embedding_scale import CONFIG

from mcp_manager.catalog_models import CatalogEntry, CatalogHead
from mcp_manager.database import McpServer
from mcp_manager.embedding_models import EmbeddingVector
from mcp_manager.embeddings import _settings, prune_stale_vectors
from mcp_manager.retention import collect_objects, run_startup_cache_cleanup


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
    assert len(await vector_rows(state)) == 1  # The model-change event already removed the old model.
    result = await run_startup_cache_cleanup(state)
    assert result["vectors"] == 0
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
    await run_startup_cache_cleanup(state)
    assert [(v.generation_id, v.entry_id, v.model_hash) for v in await vector_rows(state)] == before


async def test_retention_removes_unreferenced_generation_vectors_in_bounded_batches(db_app, vector_catalog):
    _row, scope, _previous = vector_catalog
    state = db_app.app.state
    original = (await vector_rows(state))[0]
    historical = {column.name: getattr(original, column.name) for column in EmbeddingVector.__table__.columns}
    fresh = await state.catalog.index.begin_refresh(scope)
    await state.catalog.index.stage_page(fresh.id, "tools", [{"name": "new", "inputSchema": {"type": "object"}}])
    await state.catalog.index.publish(fresh.id, expected_scope=scope)
    async with state.db.locked() as session:
        await session.execute(insert(EmbeddingVector).values(**historical))
    result = await prune_stale_vectors(state, batch_limit=1, startup=True)
    assert result["vectors"] <= 1
    assert result["vectors"] == 1
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
    assert value["cache_version"] == 2
    assert value["references"] == [[row.id, row.revision]]
    assert value["model_hash"] == state.embeddings._model_hash(endpoint, model)


async def test_retention_rebuilds_opaque_legacy_cache_and_compacts_catalog(db_app, vector_catalog):
    state = db_app.app.state
    store = state.embeddings.store
    store.set("opaque-legacy", {"vector": [1.0, 1.0]})
    opaque_id, opaque_size = store.connection.execute(
        "SELECT artifact_id,size_bytes FROM cache_items WHERE domain=? AND key=?",
        ("embedding", "opaque-legacy")).fetchone()
    catalog = state.catalog.cache_store
    row = vector_catalog[0]
    key = state.catalog.cache_key(row)
    catalog.set(key, {"revision": row.revision, "data": {}})
    catalog.set("gone", {"payload": "x" * 4096})
    gone_id, gone_size = catalog.connection.execute(
        "SELECT artifact_id,size_bytes FROM cache_items WHERE domain=? AND key=?",
        ("catalog", "gone")).fetchone()
    catalog.set("gone", None)
    await run_startup_cache_cleanup(state)
    assert store.get("opaque-legacy") is None
    assert catalog.get("gone") is None
    state.catalog.artifacts.gc_grace_seconds = 0
    await collect_objects(state)
    assert not state.catalog.artifacts._path(opaque_id).exists()
    assert not state.catalog.artifacts._path(gone_id).exists()
    assert opaque_size > 0 and gone_size > 0
    assert catalog.get(key) is not None
    assert await vector_rows(state), "Shared index cleanup must preserve live vectors"


@pytest.mark.parametrize("protected", ["disabled", "failed"])
async def test_scoped_legacy_cache_is_unchanged_for_disabled_or_failed_services(db_app, vector_catalog, protected):
    row, _, generation = vector_catalog
    state = db_app.app.state
    await state.embeddings.scores("scope", [{"id": "doc", "text": "weather", "cache_ref": [row.id, row.revision]}],
                                  "forecast", CONFIG)
    before = dict(state.embeddings.store.items)
    async with state.db.locked() as session:
        if protected == "disabled":
            (await session.get(McpServer, row.id)).mode = "disabled"
        else:
            head = await session.scalar(select(CatalogHead).where(CatalogHead.generation_id == generation))
            head.status = "error"
    await db_app.web.patch("/api/v1/settings/embedding", json=dict(CONFIG, model="changed"))
    await run_startup_cache_cleanup(state)
    assert state.embeddings.store.items == before


async def test_deleted_service_legacy_vectors_are_physically_reclaimed(db_app, vector_catalog):
    row, _, _ = vector_catalog
    state = db_app.app.state
    await state.embeddings.scores("scope", [{"id": "doc", "text": "weather", "cache_ref": [row.id, row.revision]}],
                                  "forecast", CONFIG)
    async with state.db.locked() as session:
        await session.delete(await session.get(McpServer, row.id))
    await run_startup_cache_cleanup(state)
    assert state.embeddings.store.items == {}
    state.catalog.artifacts.gc_grace_seconds = 0
    await collect_objects(state)
    assert state.embeddings.store.connection.execute(
        "SELECT count(*) FROM cache_items WHERE domain='embedding'").fetchone()[0] == 0
    assert state.embeddings.store.path.is_file(), "The shared derived database remains usable"


async def test_retention_file_failure_preserves_snapshots_and_can_retry(db_app, vector_catalog, monkeypatch):
    state = db_app.app.state
    store = state.embeddings.store
    store.set("opaque", {"vector": [1.0, 1.0]})
    before = dict(store.items)

    def blocked(keys):
        raise OSError("locked")

    with monkeypatch.context() as patch:
        patch.setattr(store, "delete_keys", blocked)
        with pytest.raises(OSError):
            await run_startup_cache_cleanup(state)
    assert dict(store.items) == before
    await run_startup_cache_cleanup(state)
    assert store.items == {}


async def test_cache_metadata_cleanup_runs_off_the_event_loop(db_app, vector_catalog, monkeypatch):
    state = db_app.app.state
    loop = asyncio.get_running_loop()
    calls = []
    original = state.catalog.cache_store.metadata_page

    def threaded(**kwargs):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        calls.append(True)
        return original(**kwargs)

    monkeypatch.setattr(state.catalog.cache_store, "metadata_page", threaded)
    await run_startup_cache_cleanup(state)
    assert calls and loop is asyncio.get_running_loop()


async def test_valid_vector_cache_ignores_untrusted_payload_timestamps(db_app, vector_catalog):
    import time

    row, _, _ = vector_catalog
    state = db_app.app.state
    endpoint, model, *_ = _settings(CONFIG)
    metadata = {"cache_version": 2, "model_hash": state.embeddings._model_hash(endpoint, model),
                "references": [[row.id, row.revision]], "vector": [1.0, 1.0]}
    store = state.embeddings.store
    stamp = time.time()
    store.set("expired", dict(metadata, cached_at=0))
    for index in range(4):
        store.set(f"live-{index}", dict(metadata, cached_at=stamp + index))
    await run_startup_cache_cleanup(state)
    assert set(store.items) == {"expired", "live-0", "live-1", "live-2", "live-3"}
    assert len(store.items) == 5
    assert await vector_rows(state), "Current catalog vectors survive compatibility cache cleanup"
