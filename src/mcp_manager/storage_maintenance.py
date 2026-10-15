"""Bounded SQLite space checks; compaction never expires application data."""
from __future__ import annotations

import asyncio
import contextlib
import math
import shutil
import sqlite3
from pathlib import Path

from .database import get_setting

SETTING_KEY = "sqlite_maintenance"
DEFAULT_POLICY = {"startup_enabled": True, "min_free_mib": 32, "min_free_percent": 20}


def normalize_policy(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError("Unknown database maintenance setting")
    result = DEFAULT_POLICY | value
    if not isinstance(result["startup_enabled"], bool):
        raise ValueError("startup_enabled must be a boolean")  # noqa: TRY004 - API validation contract
    for key, maximum in (("min_free_mib", 1024**3), ("min_free_percent", 100)):
        number = result[key]
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or not 0 <= number <= maximum):
            raise ValueError(f"{key} must be a finite number from 0 to {maximum}")
    return result


def eligible(stats, policy):
    return (stats["free_bytes"] > 0
            and stats["free_bytes"] >= policy["min_free_mib"] * 1024**2
            and stats["free_percent"] >= policy["min_free_percent"])


def _connect(path, *, writable=False):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError("SQLite database is not available")
    # mode=rw prevents accidental creation; mode=ro makes status strictly read-only.
    return sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                           uri=True, timeout=0, isolation_level=None)


def _snapshot(conn, path):
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    free = min(pages, conn.execute("PRAGMA freelist_count").fetchone()[0])
    def size(candidate):
        try:
            return candidate.stat().st_size
        except FileNotFoundError:
            return 0
    file_bytes = size(path)
    wal_bytes = size(Path(str(path) + "-wal"))
    return {"page_bytes": pages * page_size, "free_bytes": free * page_size,
            "free_percent": 100 * free / pages if pages else 0,
            "file_bytes": file_bytes, "wal_bytes": wal_bytes,
            "total_bytes": file_bytes + wal_bytes}


def sqlite_stats(path):
    path = Path(path)
    with contextlib.closing(_connect(path)) as conn:
        return _snapshot(conn, path)


def vacuum_sqlite(path, *, policy=None):
    """Fail fast on active writers/readers; SQLite owns WAL/SHM lifecycle."""
    path = Path(path)
    with contextlib.closing(_connect(path, writable=True)) as conn:
        before = _snapshot(conn, path)
        result = {"before": before, "reclaimed_bytes": 0}
        if policy is not None and not eligible(before, policy):
            return result | {"status": "below_threshold", "after": before}
        # VACUUM can need up to twice the original DB size in additional free space.
        if shutil.disk_usage(path.parent).free < 2 * before["page_bytes"]:
            return result | {"status": "insufficient_space", "after": before}
        try:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint[0]:
                return result | {"status": "busy", "after": _snapshot(conn, path)}
            conn.execute("VACUUM")
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                return result | {"status": "busy", "after": _snapshot(conn, path)}
            raise
        after = _snapshot(conn, path)
        return result | {"status": "completed", "after": after,
                         "checkpoint_pending": bool(checkpoint[0]),
                         "reclaimed_bytes": max(0, before["total_bytes"] - after["total_bytes"])}


class StorageMaintenance:
    def __init__(self, state):
        self.state = state
        self.locks = {name: asyncio.Lock() for name in ("main", "logs")}
        self.started = False
        self.last_results = {}

    def path(self, target):
        if target == "logs":
            return Path(self.state.config.data_dir) / "indexes" / "logs.sqlite"
        if target != "main":
            raise KeyError(target)
        engine = self.state.db.engine
        database = engine.url.database
        if engine.dialect.name != "sqlite" or not database or database == ":memory:":
            return None
        return Path(database)

    async def policies(self):
        saved = await get_setting(self.state.db, SETTING_KEY, {})
        return {name: normalize_policy(saved.get(name, {})) for name in self.locks}

    async def describe(self):
        policies = await self.policies()
        databases = {}
        for target in self.locks:
            path = self.path(target)
            item = {"supported": path is not None,
                    "backend": "sqlite" if path else self.state.db.engine.dialect.name,
                    "policy": policies[target] if path else None,
                    "stats": None, "running": self.locks[target].locked(),
                    "last_result": self.last_results.get(target)}
            if path:
                try:
                    item["stats"] = await asyncio.to_thread(sqlite_stats, path)
                except (sqlite3.Error, OSError):
                    item["stats_error"] = "暂时无法读取空间信息，请稍后重试"
            databases[target] = item
        return {"databases": databases}

    def _compact(self, target, path, policy):
        if target != "logs":
            return vacuum_sqlite(path, policy=policy)
        # Do not race the shared log connection or silently commit its transaction.
        logs = self.state.logs
        if not logs.lock.acquire(blocking=False):
            return {"status": "busy", "reclaimed_bytes": 0}
        try:
            if logs.conn.in_transaction:
                return {"status": "busy", "reclaimed_bytes": 0}
            return vacuum_sqlite(path, policy=policy)
        finally:
            logs.lock.release()

    async def run(self, target, *, automatic=False):
        path = self.path(target)
        if path is None:
            raise ValueError("This database does not support SQLite space reclamation")
        lock = self.locks[target]
        if lock.locked():
            return {"status": "busy", "reclaimed_bytes": 0}
        async with lock:
            policy = (await self.policies())[target] if automatic else None
            if policy is not None and not policy["startup_enabled"]:
                return {"status": "disabled", "reclaimed_bytes": 0}
            worker = asyncio.create_task(asyncio.to_thread(self._compact, target, path, policy))
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling a coroutine cannot stop SQLite running in a worker thread.
                # Join it before releasing the guard or closing the application DBs.
                while not worker.done():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.shield(worker)
                with contextlib.suppress(Exception):
                    worker.result()
                raise
            self.last_results[target] = result
            return result

    async def startup(self):
        if self.started:
            return
        self.started = True
        for target in self.locks:
            if self.path(target) is None:
                continue
            try:
                result = await self.run(target, automatic=True)
                if result["status"] not in {"below_threshold", "disabled"}:
                    await self.state.logs.audit("storage.startup", None,
                                                {"database": target, **result})
            except (OSError, sqlite3.Error, ValueError):
                # Space maintenance is optional; a busy/unavailable disk must not
                # prevent the configured gateway from starting.
                await self.state.logs.audit("storage.startup.error", None, {"database": target})
