"""A failed schema step leaves no unversioned DDL behind on SQLite."""
import sqlite3

import pytest

from mcp_manager.config import Settings
from mcp_manager.database import Database


async def test_derived_schema_ddl_is_rolled_back_before_retry(tmp_path, monkeypatch):
    from mcp_manager import derived_schema
    from mcp_manager.derived_index import DerivedIndex
    db = Database(Settings(data_dir=tmp_path, secret_key="ddl-fault-test"))
    db.derived = DerivedIndex(db)
    original = derived_schema.migrate_derived_schema
    def fail(connection):
        connection.exec_driver_sql("CREATE TABLE interrupted_upgrade(id INTEGER PRIMARY KEY)")
        raise RuntimeError("injected DDL failure")
    monkeypatch.setattr(derived_schema, "migrate_derived_schema", fail)
    try:
        with pytest.raises(RuntimeError, match="DDL failure"):
            await db.derived.initialize()
        with sqlite3.connect(tmp_path / "indexes/derived.sqlite") as connection:
            assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='interrupted_upgrade'").fetchone() is None
        monkeypatch.setattr(derived_schema, "migrate_derived_schema", original)
        await db.derived.initialize()
    finally:
        await db.close()
