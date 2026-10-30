"""A pinned WAL pauses disposable writes without deleting sidecars or durable results."""
import sqlite3

import pytest

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.runtime import GatewayError
from mcp_manager.storage_policy import MIB


def test_pinned_wal_backpressures_cache_but_preserves_persistent_capacity(tmp_path):
    store = ArtifactStore(tmp_path)
    durable = store.for_namespace("durable")
    try:
        store.admission.set_policy({"wal_warn_bytes": MIB, "wal_stop_bytes": MIB,
                                    "min_free_bytes": 0, "min_free_percent": 0})
        with sqlite3.connect(store.inventory.path) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE pressure_test(body BLOB)")
            writer.commit()
            reader = sqlite3.connect(store.inventory.path)
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM pressure_test").fetchone()
            writer.execute("INSERT INTO pressure_test VALUES(?)", [b"x" * (2 * MIB)])
            writer.commit()
            wal = store.inventory.path.with_name(store.inventory.path.name + "-wal")
            before = wal.stat().st_size
            assert before > MIB
            with pytest.raises(GatewayError) as exc:
                store._put_json({"disposable": True}, MIB)
            assert exc.value.code == "wal_pressure"
            saved = durable._put_json({"result": "keep"}, MIB)
            assert durable._path(saved.id).is_file()
            assert wal.stat().st_size >= before
            assert store.admission.usage()["slots"] == 0
            reader.rollback()
            reader.close()
            assert store._put_json({"disposable": "after checkpoint"}, MIB).size_bytes > 0
            assert wal.exists()
    finally:
        durable.close()
        store.close()
