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


class DerivedIndex:
    """Separate pooled sessions, with explicit routing for authority lookups."""
    def __init__(self, database):
        from sqlalchemy import event
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from .database import Base
        from .db_types import strict_json_dumps

        self.path = prepare_index_path(database.settings.data_dir)
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///" + self.path.as_posix(), json_serializer=strict_json_dumps,
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure(connection, record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        main_bindings = {table: database.engine for table in Base.metadata.tables.values()}
        self._sessions = async_sessionmaker(self.engine, binds=main_bindings, expire_on_commit=False)
        from .storage_gate import StorageGate
        self.gate = StorageGate(database.settings.data_dir)

    @contextlib.asynccontextmanager
    async def session(self):
        # Read snapshots rely on SQLite WAL locks. They do not acquire a
        # process-wide file admission lock for every metadata lookup.
        async with self._sessions() as session:
            yield session

    async def initialize(self):
        from .derived_schema import migrate_derived_schema
        async with self.gate.async_operation(), self.engine.begin() as connection:
            # sqlite3 legacy transaction mode does not BEGIN for DDL. A real
            # write transaction must precede schema steps and version markers.
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await connection.run_sync(migrate_derived_schema)

    @contextlib.asynccontextmanager
    async def locked(self):
        from sqlalchemy import text

        async with self.gate.async_operation(), self._sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def close(self):
        await self.engine.dispose()
        self.gate.close()
