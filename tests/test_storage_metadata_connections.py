"""Tiny immutable bodies reuse a bounded number of disposable metadata connections."""
import sqlite3

import pytest

from mcp_manager.artifact_store import ArtifactStore


def test_cas_writes_reuse_connections_without_hiding_peer_policy_changes(tmp_path, monkeypatch):
    opened = []
    original = sqlite3.connect
    def connect(*args, **kwargs):
        opened.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connect)
    store = ArtifactStore(tmp_path)
    try:
        for index in range(10):
            store._put_json({"value": index}, 65536)
        assert len(opened) <= 3, "Each small body reopened metadata connections"
    finally:
        store.close()


def test_inventory_schema_failure_closes_its_new_connection(tmp_path, monkeypatch):
    from mcp_manager import object_inventory
    from mcp_manager.object_inventory import ObjectInventory

    connections = []
    connect = sqlite3.connect
    def recording(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection
    def failed(connection):
        raise RuntimeError("schema fault")
    monkeypatch.setattr(sqlite3, "connect", recording)
    monkeypatch.setattr(object_inventory, "migrate_object_schema", failed)
    with pytest.raises(RuntimeError, match="schema fault"):
        ObjectInventory(tmp_path)
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
