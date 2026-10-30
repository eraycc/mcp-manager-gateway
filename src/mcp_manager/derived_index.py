"""One disposable local index and a connection-safe legacy rename."""
from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path

from filelock import FileLock


def prepare_index_path(data_dir: Path, *, migrate: bool = True) -> Path:
    """Run before opening index connections, behind the runtime upgrade barrier."""
    folder = Path(data_dir).resolve() / "indexes"
    folder.mkdir(parents=True, exist_ok=True)
    locks = Path(data_dir).resolve() / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    old, target = folder / "logs.sqlite", folder / "derived.sqlite"
    with FileLock(str(locks / "derived-layout.lock"), timeout=10):
        if old.is_symlink() or target.is_symlink():
            raise RuntimeError("Index paths must not be symbolic links")
        if old.exists() and target.exists():
            raise RuntimeError("Index path conflict: both logs.sqlite and derived.sqlite exist")
        if not old.exists():
            return target
        if not migrate:
            raise RuntimeError("Legacy index requires storage migration")
        try:
            with contextlib.closing(sqlite3.connect(old, timeout=0, isolation_level=None)) as conn:
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint[0]:
                    raise RuntimeError("Legacy index has active connections; stop all writers first")
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Legacy index integrity verification failed")
        except sqlite3.OperationalError as exc:
            raise RuntimeError("Legacy index is busy; close all connections before migration") from exc
        # SQLite owns WAL/SHM removal at the final clean close. An existing
        # connection must never keep writing to the old name after the rename.
        if any(Path(str(old) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise RuntimeError("Legacy index still has active connections")
        os.rename(old, target)
        return target
