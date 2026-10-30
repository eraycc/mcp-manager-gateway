"""Destructive first cutover requires an actual offline runtime ownership guard."""
import pytest
from filelock import FileLock
from test_storage_migration import CACHE_TABLES, legacy_store, table_names

from mcp_manager.database import Database


async def test_implicit_migration_refuses_a_live_owner_before_changing_old_schema(db_settings):
    await legacy_store(db_settings)
    database = Database(db_settings)
    try:
        with FileLock(str(db_settings.data_dir / "runtime.lock"), timeout=0):
            with pytest.raises(RuntimeError, match="runtime|maintenance|writers"):
                await database.initialize()
            assert CACHE_TABLES <= await table_names(database)
    finally:
        await database.close()


async def test_declared_guard_allows_migration_and_does_not_release_callers_lease(db_settings):
    await legacy_store(db_settings)
    database = Database(db_settings)
    try:
        with FileLock(str(db_settings.data_dir / "runtime.lock"), timeout=0) as guard:
            await database.initialize(runtime_guard=guard)
            assert guard.is_locked
            assert CACHE_TABLES.isdisjoint(await table_names(database))
    finally:
        await database.close()
