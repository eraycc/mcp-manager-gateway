"""Rebuildable cache bodies are lazy CAS values with bounded process memory."""
import importlib.util


def cache_type():
    assert importlib.util.find_spec("mcp_manager.derived_cache"), "Need indexed CAS cache"
    from mcp_manager.derived_cache import DerivedCache
    return DerivedCache


def test_independent_instances_observe_replacements_without_loading_every_body(tmp_path):
    factory = cache_type()
    first = factory(tmp_path, "embedding", max_cache_bytes=2048)
    second = factory(tmp_path, "embedding", max_cache_bytes=2048)
    for number in range(20):
        first.set(str(number), {"vector": [number] * 250})
    assert second.get("1")["vector"][0] == 1
    first.set("1", {"vector": [91]})
    assert second.get("1") == {"vector": [91]}
    assert second.cache_size <= 2048
    first.set("1", None)
    assert second.get("1") is None
    assert len(second.items) == 19
    assert not (tmp_path / "indexes/embeddings.jsonl").exists()


def test_cache_bodies_stay_out_of_sql_and_expiration_is_bounded(tmp_path):
    import sqlite3
    factory = cache_type()
    store = factory(tmp_path, "embedding", ttl_seconds=1)
    store.set("query", {"vector": [0.123456789, 0.987654321], "cache_version": 2})
    with sqlite3.connect(store.path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(cache_items)")]
        assert "vector" not in columns and "value" not in columns
        conn.execute("UPDATE cache_items SET expires_at=0")
    assert store.get("query") is None
    assert store.prune(limit=1) == 1


def test_identical_permanent_cache_update_reuses_verified_body_without_staging(tmp_path, monkeypatch):
    store = cache_type()(tmp_path, "catalog")
    value = {"revision": 1, "data": {"cache_status": "ready"}}
    store.set("scope", value)
    original = store.artifacts._put_json
    writes = []
    def put(*args):
        writes.append(True)
        return original(*args)
    monkeypatch.setattr(store.artifacts, "_put_json", put)
    store.set("scope", value)
    assert writes == [], "An unchanged permanent projection must not restage its CAS body"
    assert store.get("scope") == value
    identity = store.connection.execute("SELECT artifact_id FROM cache_items WHERE key=?", ["scope"]).fetchone()[0]
    store.artifacts._path(identity).unlink()
    store.set("scope", value)
    assert writes == [True], "A missing disposable body must be repaired"
    assert store.get("scope") == value
    store.close()


def test_repeated_metadata_reads_reuse_bounded_connection_and_observe_peer(tmp_path, monkeypatch):
    import mcp_manager.derived_cache as module

    first = cache_type()(tmp_path, "catalog")
    second = cache_type()(tmp_path, "catalog")
    first.set("scope", {"revision": 1})
    original, opened = module.sqlite3.connect, []
    def connect(*args, **kwargs):
        opened.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(module.sqlite3, "connect", connect)
    for _ in range(50):
        assert first.get("scope") == {"revision": 1}
    assert len(opened) <= 2, "A hot metadata key reopened SQLite for every read"
    second.set("scope", {"revision": 2})
    assert first.get("scope") == {"revision": 2}
    first.close()
    second.close()
