"""Disposable object inventory and atomic cache-byte admission."""
import contextlib
import sqlite3
import threading
import time
from pathlib import Path

from .derived_index import prepare_index_path
from .runtime import GatewayError


def migrate_object_schema(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS object_schema_versions(version INTEGER PRIMARY KEY)")
    version = connection.execute("SELECT COALESCE(MAX(version),0) FROM object_schema_versions").fetchone()[0]
    if version == 0:
        connection.execute("""CREATE TABLE object_inventory(
            namespace TEXT NOT NULL,id TEXT NOT NULL,size_bytes INTEGER NOT NULL,
            state TEXT NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(namespace,id))""")
        connection.execute("CREATE INDEX object_inventory_gc ON object_inventory(namespace,state,updated_at,id)")
        connection.execute("""CREATE TABLE object_domain_usage(
            namespace TEXT PRIMARY KEY,body_bytes INTEGER NOT NULL,objects_count INTEGER NOT NULL,
            inventory_ready INTEGER NOT NULL DEFAULT 0)""")
        connection.execute("""CREATE TRIGGER object_usage_insert AFTER INSERT ON object_inventory BEGIN
            INSERT INTO object_domain_usage(namespace,body_bytes,objects_count) VALUES(NEW.namespace,NEW.size_bytes,1)
            ON CONFLICT(namespace) DO UPDATE SET body_bytes=body_bytes+NEW.size_bytes,objects_count=objects_count+1;
            END""")
        connection.execute("""CREATE TRIGGER object_usage_delete AFTER DELETE ON object_inventory BEGIN
            UPDATE object_domain_usage SET body_bytes=body_bytes-OLD.size_bytes,objects_count=objects_count-1
            WHERE namespace=OLD.namespace; END""")
        connection.execute("INSERT INTO object_schema_versions VALUES(1)")
        version = 1
    if version == 1:
        connection.execute("ALTER TABLE object_domain_usage ADD COLUMN roots_ready INTEGER NOT NULL DEFAULT 0")
        connection.execute("INSERT INTO object_schema_versions VALUES(2)")
    elif version != 2:
        raise RuntimeError("Unsupported object inventory version")


class ObjectInventory:
    def __init__(self, data_dir):
        from .storage_runtime import require_safe_sqlite
        require_safe_sqlite()
        self.data_dir = Path(data_dir)
        self.path = prepare_index_path(data_dir)
        self._lock = threading.RLock()
        self._connection = None
        self._closed = False
        try:
            with self.connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("BEGIN IMMEDIATE")
                migrate_object_schema(connection)
                from .storage_admission import migrate_admission_schema
                migrate_admission_schema(connection)
                for namespace in ("cache", "durable"):
                    row = connection.execute("SELECT 1 FROM object_domain_usage WHERE namespace=?", [namespace]).fetchone()
                    if row is None:
                        root = self.data_dir / "objects" / namespace / "sha256"
                        empty = next(root.glob("*/*/*.json"), None) is None
                        connection.execute("INSERT INTO object_domain_usage VALUES(?,0,0,?,?)",
                                           [namespace, int(empty), int(empty or namespace == "durable")])
                connection.commit()
        except BaseException:
            self.close()
            raise

    @contextlib.contextmanager
    def connect(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Object inventory is closed")
            if self._connection is None:
                connection = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
                try:
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.execute("PRAGMA busy_timeout=5000")
                    connection.execute("PRAGMA synchronous=NORMAL")
                except BaseException:
                    connection.close()
                    raise
                self._connection = connection
            try:
                yield self._connection
            finally:
                if self._connection.in_transaction:
                    self._connection.rollback()

    def close(self):
        with self._lock:
            self._closed = True
            for iterator in getattr(self, "_scan_cursors", {}).values():
                iterator.close()
            getattr(self, "_scan_cursors", {}).clear()
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def reserve(self, ref, quota_bytes):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            usage = connection.execute("SELECT body_bytes,inventory_ready FROM object_domain_usage WHERE namespace=?",
                                       [ref.namespace]).fetchone()
            previous = connection.execute("SELECT size_bytes FROM object_inventory WHERE namespace=? AND id=?",
                                          [ref.namespace, ref.id]).fetchone()
            if previous and previous[0] != ref.size_bytes:
                raise GatewayError("artifact_corrupt", "Object inventory size disagrees with content identity")
            if quota_bytes is None:
                import json
                policy = json.loads(connection.execute("SELECT data FROM storage_policy_snapshot WHERE id=1").fetchone()[0])
                quota_bytes = policy[ref.namespace + "_cas_bytes"]
            if ref.namespace == "cache":
                if not usage[1]:
                    raise GatewayError("inventory_rebuilding", "Cache inventory is rebuilding; retry read-only discovery later")
                if previous is None and usage[0] + ref.size_bytes > quota_bytes:
                    raise GatewayError("cache_capacity", "Cache byte quota reached; cleanup or increase the configured quota")
            elif quota_bytes and previous is None and usage[0] + ref.size_bytes > quota_bytes:
                raise GatewayError("durable_capacity", "Durable byte quota reached; explicitly remove expired results or increase policy")
            connection.execute("""INSERT OR IGNORE INTO object_inventory VALUES(?,?,?,'reserved',?)""",
                               [ref.namespace, ref.id, ref.size_bytes, time.time()])
            connection.commit()

    def confirm(self, ref):
        with self.connect() as connection:
            connection.execute("UPDATE object_inventory SET state='present',updated_at=? WHERE namespace=? AND id=?",
                               [time.time(), ref.namespace, ref.id])

    def forget(self, namespace, identity):
        with self.connect() as connection:
            connection.execute("DELETE FROM object_inventory WHERE namespace=? AND id=?", [namespace, identity])

    def register(self, ref):
        # Used only by an inventory scan or the offline verified migration.
        with self.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO object_inventory VALUES(?,?,?,'present',?)",
                               [ref.namespace, ref.id, ref.size_bytes, time.time()])

    def ready(self, namespace):
        with self.connect() as connection:
            return bool(connection.execute("SELECT inventory_ready FROM object_domain_usage WHERE namespace=?",
                                           [namespace]).fetchone()[0])

    def finish_scan(self, namespace):
        with self.connect() as connection:
            connection.execute("UPDATE object_domain_usage SET inventory_ready=1 WHERE namespace=?", [namespace])

    def register_many(self, refs):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany("INSERT OR IGNORE INTO object_inventory VALUES(?,?,?,'present',?)",
                [(ref.namespace, ref.id, ref.size_bytes, time.time()) for ref in refs])
            connection.commit()

    def scan_next(self, namespace, *, limit=250):
        from .catalog_types import ArtifactRef
        cursors = getattr(self, "_scan_cursors", None)
        if cursors is None:
            self._scan_cursors = cursors = {}
        if namespace not in cursors:
            root = self.data_dir / "objects" / namespace / "sha256"
            cursors[namespace] = root.glob("*/*/*.json")
        iterator, refs = cursors[namespace], []
        complete = False
        for _ in range(limit):
            path = next(iterator, None)
            if path is None:
                complete = True
                break
            identity = path.stem
            if len(identity) != 64 or any(char not in "0123456789abcdef" for char in identity):
                continue
            try:
                refs.append(ArtifactRef(identity, path.stat().st_size, identity, namespace))
            except FileNotFoundError:
                continue
        self.register_many(refs)
        if complete:
            self.finish_scan(namespace)
            cursors.pop(namespace, None)
        return {"scanned": len(refs), "complete": complete}

    def roots_ready(self, namespace):
        with self.connect() as connection:
            return bool(connection.execute("SELECT roots_ready FROM object_domain_usage WHERE namespace=?",
                                           [namespace]).fetchone()[0])

    def finish_roots(self, namespace):
        with self.connect() as connection:
            connection.execute("UPDATE object_domain_usage SET roots_ready=1 WHERE namespace=?", [namespace])
