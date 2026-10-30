"""Retention reads bounded metadata pages, never vector or catalog payloads."""
import pytest
from test_catalog_paged_api import indexed_catalog

from mcp_manager.retention import reconcile_service_cache


async def test_cleanup_uses_metadata_and_keeps_valid_entries(db_app, monkeypatch):
    state = db_app.app.state
    row, _ = await indexed_catalog(db_app, 1)
    state.catalog.cache_store.set(state.catalog.cache_key(row), {"revision": row.revision, "data": {}})
    state.embeddings.store.set("valid", {
        "cache_version": 2, "references": [[row.id, row.revision]], "model_hash": "same",
        "vector": [1.0] * 1000})
    state.embeddings.store.set("deleted", {
        "cache_version": 2, "references": [["deleted-service", 1]], "model_hash": "same",
        "vector": [1.0] * 1000})
    def forbidden(*args, **kwargs):
        raise AssertionError("Metadata-only cleanup opened a CAS payload")
    with monkeypatch.context() as patch:
        patch.setattr(state.embeddings.store, "get", forbidden)
        patch.setattr(state.catalog.cache_store, "get", forbidden)
        result = await reconcile_service_cache(state, startup=True)
    assert result["embedding_entries"] == 1
    assert state.embeddings.store.get("deleted") is None
    assert state.embeddings.store.get("valid")["vector"] == [1.0] * 1000
    assert state.catalog.cache_store.get(state.catalog.cache_key(row)) is not None


async def test_cache_metadata_page_has_a_hard_limit(db_app):
    with pytest.raises(ValueError):
        db_app.app.state.embeddings.store.metadata_page(limit=1000000)
