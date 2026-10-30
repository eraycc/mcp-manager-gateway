"""CAS-backed disposable values; no journal replay or unbounded in-memory map."""
import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import MutableMapping

from .artifact_store import ArtifactStore
from .cache_index_schema import migrate_cache_schema
from .catalog_types import ArtifactRef
from .derived_index import prepare_index_path
from .jsonl_store import windows_retry


class CacheItems(MutableMapping):
    def __init__(self, store):
        self.store = store

    def __getitem__(self, key):
        value = self.store.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        self.store.set(key, value)

    def __delitem__(self, key):
        self.store.set(key, None)

    def __iter__(self):
        previous = ""
        while True:
            with self.store.lock:
                rows = self.store.connection.execute(
                    "SELECT key FROM cache_items WHERE domain=? AND key>? ORDER BY key LIMIT 250",
                    (self.store.domain, previous)).fetchall()
            if not rows:
                return
            previous = rows[-1][0]
            yield from (row[0] for row in rows)

    def __len__(self):
        with self.store.lock:
            return self.store.connection.execute(
                "SELECT count(*) FROM cache_items WHERE domain=?", (self.store.domain,)).fetchone()[0]


class DerivedCache:
    def __init__(self, data_dir, domain, *, max_cache_bytes=2 * 1024 * 1024,
                 ttl_seconds=None, max_rows=100000, max_body_bytes=256 * 1024 * 1024):
        if domain not in {"catalog", "embedding"}:
            raise ValueError("Invalid cache domain")
        self.domain = domain
        self.path = prepare_index_path(data_dir)
        from .storage_gate import StorageGate
        self.gate = StorageGate(data_dir)
        try:
            self.lock = threading.RLock()
            self.cache_lock = threading.RLock()
            self.connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False, isolation_level=None)
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            with self.gate.operation(), self.lock:
                self.connection.execute("BEGIN IMMEDIATE")
                try:
                    migrate_cache_schema(self.connection)
                    self.connection.commit()
                except BaseException:
                    self.connection.rollback()
                    self.connection.close()
                    raise
            self.artifacts = ArtifactStore(data_dir, cache_bytes=0)
            # One bounded read-only connection stays independent of the
            # writer mutex. Autocommit sees peer commits on every PK lookup.
            self.reader_lock = threading.RLock()
            self.reader = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True,
                                          timeout=.05, isolation_level=None, check_same_thread=False)
            self.reader.execute("PRAGMA cache_size=-256")
            self.ttl_seconds = ttl_seconds
            self.max_rows, self.max_body_bytes = max_rows, max_body_bytes
            self.max_cache_bytes, self.cache_size = max(0, max_cache_bytes), 0
            self.cache = OrderedDict()
            self.items = CacheItems(self)

        except BaseException:
            if hasattr(self, "reader"):
                self.reader.close()
            if hasattr(self, "artifacts"):
                self.artifacts.close()
            if hasattr(self, "connection"):
                self.connection.close()
            self.gate.close()
            raise

    def get(self, key, default=None):
        # Compatibility getters can run on the event loop. They must never
        # wait on a writer's connection, process lock, or CAS collector stripe.
        with self.reader_lock:
            try:
                row = self.reader.execute(
                    "SELECT artifact_id,size_bytes,expires_at FROM cache_items WHERE domain=? AND key=?",
                    (self.domain, key)).fetchone()
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", 0) & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    return default
                raise
        if row is None or (row[2] is not None and row[2] <= time.time()):
            return default
        identity, size = row[:2]
        ref = ArtifactRef(identity, size, identity)
        with self.cache_lock:
            raw = self.cache.get(identity)
            if raw is not None:
                self.cache.move_to_end(identity)
        if raw is None:
            try:
                # Opening an immutable body pins its inode/handle. A concurrent
                # unlink before open is an ordinary disposable-cache miss.
                raw = self.artifacts._read_disk_raw(ref, 16 * 1024 * 1024)
            except FileNotFoundError:
                return default
            with self.cache_lock:
                if len(raw) <= self.max_cache_bytes:
                    previous = self.cache.pop(identity, None)
                    if previous is not None:
                        self.cache_size -= len(previous)
                    self.cache[identity] = raw
                    self.cache_size += len(raw)
                    while self.cache_size > self.max_cache_bytes:
                        _, previous = self.cache.popitem(last=False)
                        self.cache_size -= len(previous)
        return json.loads(raw)

    def set(self, key, value, *, ttl_seconds=None):
        if not isinstance(key, str) or not key or len(key) > 512:
            raise ValueError("Invalid cache key")
        if value is None:
            with self.gate.operation(), self.lock:
                self.connection.execute("DELETE FROM cache_items WHERE domain=? AND key=?", (self.domain, key))
            return
        lifetime = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if lifetime is None:
            # Healthy calls often repeat the same permanent compatibility view.
            # Its live root already pins the immutable body: verify it under
            # the collector stripe instead of reserving, staging and fsyncing
            # another identical copy. TTL values still renew through the writer.
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            identity = hashlib.sha256(raw).hexdigest()
            with self.artifacts._stripe(identity), self.gate.operation(), self.lock:
                previous = self.connection.execute(
                    "SELECT artifact_id,size_bytes,expires_at FROM cache_items WHERE domain=? AND key=?",
                    (self.domain, key)).fetchone()
                if previous == (identity, len(raw), None):
                    try:
                        self.artifacts._read_disk_raw(ArtifactRef(identity, len(raw), identity), 16 * 1024 * 1024)
                    except FileNotFoundError:
                        pass
                    else:
                        return
        ref = windows_retry(lambda: self.artifacts._put_json(value, 16 * 1024 * 1024))
        self.prune(limit=64)
        # Lock order is object stripe -> SQLite root commit, matching collection.
        with self.artifacts._stripe(ref.id), self.gate.operation(), self.lock:
            if not self.artifacts._path(ref.id).is_file():
                raise OSError("Cache body disappeared before reference commit")
            rows, size = self.connection.execute(
                "SELECT rows_count,body_bytes FROM cache_domain_usage WHERE domain=?", (self.domain,)
            ).fetchone() or (0, 0)
            previous = self.connection.execute(
                "SELECT size_bytes FROM cache_items WHERE domain=? AND key=?", (self.domain, key)).fetchone()
            if (rows + int(previous is None) > self.max_rows
                    or size - (previous[0] if previous else 0) + ref.size_bytes > self.max_body_bytes):
                raise OSError("Rebuildable cache quota reached; cleanup must catch up")
            stamp = time.time()
            lifetime = self.ttl_seconds if ttl_seconds is None else ttl_seconds
            expires = stamp + lifetime if lifetime is not None else None
            metadata = {key: value.get(key) for key in (
                "cache_version", "capability", "content_hash", "model_hash", "references", "revision")}
            self.connection.execute(
                """INSERT INTO cache_items(domain,key,artifact_id,size_bytes,metadata,updated_at,expires_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(domain,key) DO UPDATE SET
                   artifact_id=excluded.artifact_id,size_bytes=excluded.size_bytes,metadata=excluded.metadata,
                   updated_at=excluded.updated_at,expires_at=excluded.expires_at""",
                (self.domain, key, ref.id, ref.size_bytes, json.dumps(metadata, separators=(",", ":")), stamp, expires))
        self.prune(limit=64)

    def prune(self, *, limit=250):
        if type(limit) is not int or not 1 <= limit <= 250:
            raise ValueError("Invalid cache cleanup budget")
        with self.gate.operation(), self.lock:
            rows, size = self.connection.execute(
                "SELECT rows_count,body_bytes FROM cache_domain_usage WHERE domain=?", (self.domain,)
            ).fetchone() or (0, 0)
            over = rows > self.max_rows or size > self.max_body_bytes
            condition = "" if over else "AND expires_at IS NOT NULL AND expires_at<=?"
            parameters = (self.domain, limit) if over else (self.domain, time.time(), limit)
            keys = [row[0] for row in self.connection.execute(
                "SELECT key FROM cache_items WHERE domain=? " + condition + " ORDER BY updated_at,key LIMIT ?",
                parameters)]
            if keys:
                self.connection.executemany("DELETE FROM cache_items WHERE domain=? AND key=?",
                                            [(self.domain, key) for key in keys])
            return len(keys)

    def metadata_page(self, *, after="", limit=250):
        if type(limit) is not int or not 1 <= limit <= 250 or not isinstance(after, str) or len(after) > 512:
            raise ValueError("Invalid cache metadata page")
        with self.gate.operation(), self.lock:
            return [(key, json.loads(raw)) for key, raw in self.connection.execute(
                "SELECT key,metadata FROM cache_items WHERE domain=? AND key>? ORDER BY key LIMIT ?",
                (self.domain, after, limit))]

    def delete_keys(self, keys):
        # Callers may stream keys: retain at most one bounded SQL batch.
        count, pending = 0, []
        for key in keys:
            pending.append((self.domain, key))
            if len(pending) == 250:
                with self.gate.operation(), self.lock:
                    self.connection.executemany("DELETE FROM cache_items WHERE domain=? AND key=?", pending)
                count += len(pending)
                pending.clear()
        if pending:
            with self.gate.operation(), self.lock:
                self.connection.executemany("DELETE FROM cache_items WHERE domain=? AND key=?", pending)
            count += len(pending)
        return count

    def delete_prefix(self, prefix):
        # Prefix comparison is literal; user ids are never SQL LIKE patterns.
        with self.gate.operation(), self.lock:
            result = self.connection.execute(
                "DELETE FROM cache_items WHERE domain=? AND substr(key,1,?)=?",
                (self.domain, len(prefix), prefix))
            return result.rowcount

    def compact(self):
        # No append journal; free SQL pages are reused. Full VACUUM is explicit maintenance.
        return 0

    def close(self):
        with self.reader_lock, contextlib.suppress(sqlite3.ProgrammingError):
            self.reader.close()
        self.artifacts.close()
        with contextlib.suppress(sqlite3.ProgrammingError):
            self.connection.close()
        self.gate.close()
