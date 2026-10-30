"""Same-host bounded temporary-write reservations, with persistent domain headroom."""
import asyncio
import contextlib
import json
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from .runtime import GatewayError
from .storage_async import join_task, run_blocking
from .storage_gate import StorageGate
from .storage_policy import DEFAULT_STORAGE_POLICY, normalize_storage_policy


def migrate_admission_schema(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS storage_admission_versions(version INTEGER PRIMARY KEY)")
    version = connection.execute("SELECT COALESCE(MAX(version),0) FROM storage_admission_versions").fetchone()[0]
    if version == 0:
        connection.execute("""CREATE TABLE IF NOT EXISTS storage_policy_snapshot(
            id INTEGER PRIMARY KEY,version INTEGER NOT NULL,data TEXT NOT NULL)""")
        connection.execute("INSERT OR IGNORE INTO storage_policy_snapshot VALUES(1,1,?)",
                           (json.dumps(DEFAULT_STORAGE_POLICY, separators=(",", ":")),))
        connection.execute("""CREATE TABLE storage_stage_reservations(
            id TEXT PRIMARY KEY,owner TEXT NOT NULL,namespace TEXT NOT NULL,
            reserved_bytes INTEGER NOT NULL,created_at REAL NOT NULL)""")
        connection.execute("CREATE INDEX storage_stage_owner ON storage_stage_reservations(owner)")
        connection.execute("""CREATE TABLE storage_gc_cursors(
            domain TEXT PRIMARY KEY,position TEXT NOT NULL,updated_at REAL NOT NULL)""")
        connection.execute("INSERT INTO storage_admission_versions VALUES(1)")
        version = 1
    if version == 1:
        connection.execute("ALTER TABLE storage_stage_reservations ADD COLUMN relative_path TEXT")
        connection.execute("INSERT INTO storage_admission_versions VALUES(2)")
        version = 2
    if version != 2:
        raise RuntimeError("Unsupported storage admission schema")


class StorageAdmission:
    def __init__(self, data_dir, *, gate=None):
        self.root = Path(data_dir).resolve()
        self.path = self.root / "indexes/derived.sqlite"
        if not self.path.is_file():
            raise RuntimeError("Initialize the derived admission schema before opening writers")
        self.gate = gate or StorageGate(self.root)
        self.owns_gate = gate is None
        self.owner = uuid4().hex
        self.workers = self.root / "locks/staging-workers"
        self.workers.mkdir(parents=True, exist_ok=True)
        self.lease_path = self.workers / (self.owner + ".lock")
        self.lease = FileLock(str(self.lease_path), timeout=0, thread_local=False)
        self.lease.acquire()
        self.local = threading.RLock()
        self.active, self.closed = 0, False
        self._connection = None

    @contextlib.contextmanager
    def connect(self):
        with self.local:
            if self.closed:
                raise RuntimeError("Storage admission is closed")
            if self._connection is None:
                connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                             timeout=5, isolation_level=None, check_same_thread=False)
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

    def set_policy(self, value):
        policy = normalize_storage_policy(value)
        with self.gate.operation(), self.connect() as connection:
            connection.execute("UPDATE storage_policy_snapshot SET version=version+1,data=? WHERE id=1",
                               [json.dumps(policy, separators=(",", ":"))])
        return policy

    def policy(self):
        with self.connect() as connection:
            value = connection.execute("SELECT data FROM storage_policy_snapshot WHERE id=1").fetchone()
            if not value:
                raise RuntimeError("Storage policy mirror is unavailable")
            return normalize_storage_policy(json.loads(value[0]))

    def _check_wal(self, namespace):
        if namespace != "cache":
            return
        policy = self.policy()
        try:
            physical = Path(str(self.path) + "-wal").stat().st_size
        except FileNotFoundError:
            return
        if physical <= policy["wal_stop_bytes"]:
            return
        # A reusable WAL may remain large after a successful checkpoint. Only
        # outstanding frames, rather than its allocated length, exert pressure.
        with self.connect() as connection:
            connection.execute("PRAGMA busy_timeout=0")
            try:
                busy, frames, completed = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            finally:
                connection.execute("PRAGMA busy_timeout=5000")
        if busy or frames < 0 or (frames - completed) * (page_size + 24) >= policy["wal_stop_bytes"]:
            raise GatewayError("wal_pressure", "Derived WAL is pinned; finish long readers before rebuilding caches")

    def _check(self, connection, namespace, size, *, replacing=0, new_slot=False):
        policy = normalize_storage_policy(json.loads(connection.execute(
            "SELECT data FROM storage_policy_snapshot WHERE id=1").fetchone()[0]))
        count, reserved, disposable_count, disposable_bytes = connection.execute(
            """SELECT count(*),COALESCE(sum(reserved_bytes),0),
               COALESCE(sum(namespace='cache'),0),
               COALESCE(sum(CASE WHEN namespace='cache' THEN reserved_bytes ELSE 0 END),0)
               FROM storage_stage_reservations""").fetchone()
        delta = size - replacing
        full = (count + int(new_slot) > policy["staging_slots"]
                or reserved + delta > policy["staging_bytes"])
        if namespace == "cache":
            full |= (disposable_count + int(new_slot) > policy["staging_slots"] - policy["durable_reserve_slots"]
                     or disposable_bytes + delta > policy["staging_bytes"] - policy["durable_reserve_bytes"])
        if full:
            raise GatewayError("staging_capacity", "Temporary storage budget is busy; retry a rebuildable task later")
        disk = shutil.disk_usage(self.root)
        low_water = max(policy["min_free_bytes"], disk.total * policy["min_free_percent"] / 100)
        if disk.free < low_water + reserved + delta:
            raise GatewayError("storage_low_space", "Storage free-space reserve reached; release space or adjust policy")

    @contextlib.contextmanager
    def reserve(self, namespace, max_bytes):
        if namespace not in {"cache", "durable", "calls", "audit"} or type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("Invalid staging reservation")
        identity = uuid4().hex
        with self.gate.operation():
            with self.local:
                if self.closed:
                    raise RuntimeError("Storage admission is closed")
                self._check_wal(namespace)
                with self.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._check(connection, namespace, max_bytes, new_slot=True)
                    connection.execute("INSERT INTO storage_stage_reservations(id,owner,namespace,reserved_bytes,created_at) VALUES(?,?,?,?,?)",
                                       [identity, self.owner, namespace, max_bytes, time.time()])
                    connection.commit()
                self.active += 1
            try:
                yield identity
            finally:
                with self.local:
                    try:
                        with self.connect() as connection:
                            connection.execute("DELETE FROM storage_stage_reservations WHERE id=? AND owner=?",
                                               [identity, self.owner])
                    finally:
                        self.active -= 1

    @contextlib.asynccontextmanager
    async def async_reserve(self, namespace, size):
        reservation = self.reserve(namespace, size)
        opening = asyncio.create_task(asyncio.to_thread(reservation.__enter__))
        try:
            identity = await asyncio.shield(opening)
        except asyncio.CancelledError:
            try:
                await join_task(opening)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001,S110 - preserve owner cancellation after joining
                pass
            else:
                await run_blocking(reservation.__exit__, None, None, None)
            raise
        try:
            yield identity
        finally:
            await run_blocking(reservation.__exit__, None, None, None)

    def grow(self, identity, size):
        """Reserve the next bounded chunk before writing it; failed growth changes nothing."""
        if not isinstance(identity, str) or type(size) is not int or size < 1:
            raise ValueError("Invalid reservation growth")
        with self.gate.operation(), self.local, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT namespace,reserved_bytes FROM storage_stage_reservations WHERE id=? AND owner=?",
                [identity, self.owner]).fetchone()
            if row is None:
                raise ValueError("Reservation is no longer owned by this writer")
            if size > row[1]:
                self._check(connection, row[0], size, replacing=row[1])
                connection.execute("UPDATE storage_stage_reservations SET reserved_bytes=? WHERE id=? AND owner=?",
                                   [size, identity, self.owner])
            connection.commit()

    def _spool_paths(self, identity, namespace, relative=None):
        if not re.fullmatch("[0-9a-f]{32}", identity) or namespace not in {"cache", "durable", "calls", "audit"}:
            raise ValueError("Invalid temporary spool identity")
        root = (self.root / "objects" / namespace / "sha256" if namespace in {"cache", "durable"}
                else self.root / "logs" / namespace)
        if not root.resolve().is_relative_to(self.root):
            raise ValueError("Temporary spool root leaves the data directory")
        original = root / ("." + identity + ".tmp")
        paths = [original]
        if relative is not None:
            path = self.root / relative
            suffix = path.relative_to(root).parts
            if (path.name != original.name or len(suffix) not in ({1, 3} if namespace in {"cache", "durable"} else {1})
                    or len(suffix) == 3 and not all(re.fullmatch("[0-9a-f]{2}", part) for part in suffix[:2])
                    or not path.resolve().is_relative_to(root.resolve())):
                raise ValueError("Temporary spool path leaves its managed domain")
            paths.append(path)
        return paths

    def set_path(self, identity, path):
        path = Path(path).absolute()
        relative = path.relative_to(self.root).as_posix()
        with self.gate.operation(), self.local, self.connect() as connection:
            row = connection.execute(
                "SELECT namespace FROM storage_stage_reservations WHERE id=? AND owner=?",
                [identity, self.owner]).fetchone()
            if row is None:
                raise ValueError("Reservation is no longer owned by this writer")
            self._spool_paths(identity, row[0], relative)
            connection.execute("UPDATE storage_stage_reservations SET relative_path=? WHERE id=? AND owner=?",
                               [relative, identity, self.owner])

    def usage(self):
        with self.connect() as connection:
            count, size = connection.execute(
                "SELECT count(*),COALESCE(sum(reserved_bytes),0) FROM storage_stage_reservations").fetchone()
        return {"slots": count, "bytes": size}

    def reap_abandoned(self, *, limit=32):
        if type(limit) is not int or not 1 <= limit <= 250:
            raise ValueError("Invalid reservation recovery budget")
        with self.gate.operation(), self.connect() as connection:
            saved = connection.execute(
                "SELECT position FROM storage_gc_cursors WHERE domain='staging'").fetchone()
            previous = saved[0] if saved else ""
            rows = connection.execute(
                "SELECT id,owner,namespace,relative_path FROM storage_stage_reservations WHERE id>? ORDER BY id LIMIT ?",
                [previous, limit]).fetchall()
            removed = 0
            for identity, owner, namespace, relative in rows:
                if not re.fullmatch("[0-9a-f]{32}", owner):
                    raise RuntimeError("Invalid staging worker identity")
                if owner == self.owner:
                    continue
                lease_path = self.workers / (owner + ".lock")
                lease = FileLock(str(lease_path), timeout=0, thread_local=False)
                try:
                    lease.acquire()
                except Timeout:
                    continue
                else:
                    try:
                        if namespace in {"cache", "durable", "calls", "audit"}:
                            for spool_path in self._spool_paths(identity, namespace, relative):
                                spool_path.unlink(missing_ok=True)
                        if connection.execute("SELECT 1 FROM sqlite_master WHERE name=\'call_rewrite_offsets\'").fetchone():
                            connection.execute("DELETE FROM call_rewrite_offsets WHERE rewrite_id=?", [identity])
                        connection.execute("DELETE FROM storage_stage_reservations WHERE id=? AND owner=?", [identity, owner])
                        removed += 1
                    finally:
                        lease.release()
                    lease_path.unlink(missing_ok=True)
            position = rows[-1][0] if len(rows) == limit else ""
            connection.execute("""INSERT INTO storage_gc_cursors VALUES('staging',?,?)
                ON CONFLICT(domain) DO UPDATE SET position=excluded.position,updated_at=excluded.updated_at""",
                [position, time.time()])
            return {"scanned": len(rows), "released": removed}

    def close(self):
        with self.local:
            if self.closed:
                return
            if self.active:
                raise RuntimeError("Cannot close staging admission with active writes")
            self.closed = True
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self.lease.release()
            self.lease_path.unlink(missing_ok=True)
            if self.owns_gate:
                self.gate.close()
