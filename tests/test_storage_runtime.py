"""Fixed engines are quiet; older engines recommend an upgrade and remain usable."""
import logging
import sqlite3

import pytest
from sqlalchemy import text

from mcp_manager.config import Settings
from mcp_manager.database import Database
from mcp_manager.storage_runtime import warn_sqlite_runtime


@pytest.mark.parametrize("version", [(3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 53, 1)])
def test_fixed_sqlite_versions(version, monkeypatch, caplog):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    with caplog.at_level(logging.WARNING):
        assert warn_sqlite_runtime()
    assert not caplog.records


@pytest.mark.parametrize("version", [(3, 44, 5), (3, 45, 3), (3, 50, 4), (3, 51, 2)])
async def test_unfixed_engine_warns_and_initializes_database(tmp_path, version, monkeypatch, caplog):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    db = Database(Settings(data_dir=tmp_path, secret_key="version-warning"))
    try:
        with caplog.at_level(logging.WARNING):
            await db.initialize()
        assert "startup will continue" in caplog.text and "updating Python with uv" in caplog.text
        async with db.session() as session:
            assert await session.scalar(text("SELECT 1")) == 1
        async with db.derived.session() as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await db.close()
