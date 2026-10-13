"""Semantic retrieval embeds documents outside interactive query paths."""
import asyncio  # noqa: I001 -- keep stdlib/pytest/project groups explicit in this regression module.

import pytest
from sqlalchemy import select

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.catalog_store import CatalogStore
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.database import McpServer
from mcp_manager.embedding_models import EmbeddingVector


CONFIG = {
    "enabled": True,
    "base_url": "https://embedding.invalid/v1",
    "model": "test-model",
    "api_key": "",
    "timeout_seconds": 2.0,
    "min_similarity": -1.0,
}


async def prepared_catalog(dual_db, db_settings):
    async with dual_db.locked() as session:
        row = McpServer(
            name="Embedding service",
            slug="embedding-service",
            transport="stdio",
            config={},
            isolation="service",
        )
        session.add(row)
        await session.flush()
    artifacts = ArtifactStore(db_settings.data_dir)
    store = CatalogStore(dual_db, artifacts, signing_key=db_settings.jwt_secret)
    scope = CatalogScope(row.id, "service", row.revision, "shared")
    generation = await store.begin_refresh(scope)
    await store.stage_page(
        generation.id,
        "tools",
        [
            {
                "name": f"tool-{index}",
                "description": "weather forecast" if index == 0 else f"utility {index}",
                "inputSchema": {"type": "object"},
            }
            for index in range(70)
        ],
    )
    await store.publish(generation.id, expected_scope=scope)
    _total, candidates = await store.query_tool_entries(
        [generation.id], query="weather", limit=200
    )
    return artifacts, store, scope, generation, candidates


@pytest.mark.asyncio
async def test_large_directory_does_not_build_vectors_in_query(dual_db, db_settings, monkeypatch):
    from mcp_manager.embeddings import EmbeddingIndex

    artifacts, _store, scope, generation, candidates = await prepared_catalog(dual_db, db_settings)
    embeddings = EmbeddingIndex(db_settings.data_dir, db=dual_db, artifacts=artifacts)
    calls = []

    async def fake_embed(_endpoint, _model, _api_key, _timeout, inputs, *, authorize=None):
        if authorize:
            await authorize()
        calls.append(list(inputs))
        return [[float(len(value) + 1), 1.0] for value in inputs]

    monkeypatch.setattr(embeddings, "_embed", fake_embed)
    built = await embeddings.build_generation_vectors(scope, generation.id, CONFIG)
    assert built["built"] == 70
    assert calls and max(map(len, calls)) <= 32

    calls.clear()
    authorizations = 0

    async def authorize():
        nonlocal authorizations
        authorizations += 1

    scores, status = await embeddings.score_candidates(
        scope, candidates, "weather", CONFIG, deadline=2.0, authorize=authorize
    )
    assert scores
    assert status["semantic_status"] == "ready"
    assert calls == [["weather"]]
    assert authorizations >= 2

    calls.clear()
    cold_config = dict(CONFIG, model="not-built")
    cold_scores, cold_status = await embeddings.score_candidates(
        scope, candidates, "weather", cold_config, deadline=2.0
    )
    assert cold_scores == {}
    assert cold_status == {
        "mode": "keyword",
        "semantic_status": "unavailable",
        "reason": "cold_index",
    }
    assert calls == []


@pytest.mark.asyncio
async def test_semantic_timeout_and_revocation_fall_back_safely(dual_db, db_settings, monkeypatch):
    from mcp_manager.embeddings import EmbeddingIndex
    from mcp_manager.runtime import GatewayError

    artifacts, _store, scope, generation, candidates = await prepared_catalog(dual_db, db_settings)
    embeddings = EmbeddingIndex(db_settings.data_dir, db=dual_db, artifacts=artifacts)

    async def fake_embed(_endpoint, _model, _api_key, _timeout, inputs, *, authorize=None):
        if authorize:
            await authorize()
        return [[float(len(value) + 1), 1.0] for value in inputs]

    monkeypatch.setattr(embeddings, "_embed", fake_embed)
    await embeddings.build_generation_vectors(scope, generation.id, CONFIG)

    async def slow_embed(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return [[1.0, 1.0]]

    monkeypatch.setattr(embeddings, "_embed", slow_embed)
    scores, status = await embeddings.score_candidates(
        scope, candidates, "weather", CONFIG, deadline=0.001
    )
    assert scores == {}
    assert status["mode"] == "keyword"
    assert status["reason"] == "timeout"

    monkeypatch.setattr(embeddings, "_embed", fake_embed)
    checks = 0

    async def revoked():
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise GatewayError("unauthorized", "revoked")

    with pytest.raises(GatewayError, match="revoked"):
        await embeddings.score_candidates(
            scope, candidates, "weather", CONFIG, deadline=2.0, authorize=revoked
        )


@pytest.mark.asyncio
async def test_pending_builder_resumes_partially_built_generation(dual_db, db_settings, monkeypatch):
    from mcp_manager.embeddings import EmbeddingIndex

    artifacts, _store, scope, generation, _candidates = await prepared_catalog(dual_db, db_settings)
    embeddings = EmbeddingIndex(db_settings.data_dir, db=dual_db, artifacts=artifacts)

    async def fake_embed(_endpoint, _model, _api_key, _timeout, inputs, *, authorize=None):
        if authorize:
            await authorize()
        return [[float(len(value) + 1), 1.0] for value in inputs]

    monkeypatch.setattr(embeddings, "_embed", fake_embed)
    assert (await embeddings.build_generation_vectors(scope, generation.id, CONFIG))["built"] == 70

    async with dual_db.locked() as session:
        vector = await session.scalar(
            select(EmbeddingVector).where(EmbeddingVector.generation_id == generation.id).limit(1)
        )
        await session.delete(vector)

    resumed = await embeddings.build_pending(CONFIG)
    assert resumed == {"status": "ready", "generations": 1, "built": 1}
