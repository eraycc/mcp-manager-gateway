"""Fixed backports are accepted; unfixed engines fail before schema mutation."""
import sqlite3

import pytest

from mcp_manager.config import Settings
from mcp_manager.database import Database
from mcp_manager.storage_runtime import require_safe_sqlite


@pytest.mark.parametrize("version", [(3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 53, 1)])
def test_fixed_sqlite_versions(version, monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    require_safe_sqlite()


@pytest.mark.parametrize("version", [(3, 44, 5), (3, 45, 3), (3, 50, 4), (3, 51, 2)])
async def test_unfixed_engine_does_not_open_or_migrate_database(tmp_path, version, monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    config = Settings(data_dir=tmp_path, secret_key="version-guard")
    db = Database(config)
    try:
        with pytest.raises(RuntimeError, match="WAL-reset"):
            await db.initialize()
        assert not list(tmp_path.rglob("*.sqlite"))
    finally:
        await db.close()
