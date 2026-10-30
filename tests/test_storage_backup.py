"""Logical backup evidence detects truncation and includes restorable schema."""
import json

import pytest

from mcp_manager.storage_backup import verify_logical_backup


def test_backup_rejects_incomplete_or_changed_records(tmp_path):
    path = tmp_path / "backup.jsonl"
    path.write_text(json.dumps({"format": "mmg-main-backup", "version": 1}) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="incomplete"):
        verify_logical_backup(path)


async def test_logical_backup_restores_every_table_and_refuses_nonempty_target(dual_db, db_settings, tmp_path):
    from contextlib import AsyncExitStack

    from db_fixture import isolated_mysql_schema
    from sqlalchemy import MetaData
    from sqlalchemy.ext.asyncio import create_async_engine

    from mcp_manager.database import User
    from mcp_manager.storage_backup import restore_logical_backup, write_logical_backup

    async with dual_db.locked() as session:
        session.add(User(username="备份用户", password_hash="sealed-fixture"))
    metadata = MetaData()
    async with dual_db.engine.connect() as connection:
        await connection.run_sync(metadata.reflect)
    path = tmp_path / "logical.jsonl"
    evidence = await write_logical_backup(dual_db.engine, metadata.tables, path, run_id="fixture")
    assert "alembic_version" in evidence["header"]["tables"]
    assert "CREATE TABLE" in evidence["header"]["tables"]["users"]["create"]
    async with AsyncExitStack() as stack:
        if dual_db.engine.dialect.name == "mysql":
            url = await stack.enter_async_context(isolated_mysql_schema(db_settings.database_url))
        else:
            url = "sqlite+aiosqlite:///" + (tmp_path / "restored.sqlite").as_posix()
        target = create_async_engine(url)
        try:
            restored = await restore_logical_backup(target, path)
            assert restored["verified"] and restored["tables"] == evidence["tables"]
            from sqlalchemy import text
            async with target.begin() as connection:
                before = await connection.scalar(text("SELECT version FROM _catalog_clock WHERE id=1"))
                await connection.execute(text("UPDATE users SET email='restored@example.test'"))
                # User ACLs are read directly, while service metadata uses the clock.
                await connection.execute(text(
                    "INSERT INTO system_settings(`key`,value) VALUES('credential:restore:service','{}')"))
                after = await connection.scalar(text("SELECT version FROM _catalog_clock WHERE id=1"))
                assert after > before, "Restore must preserve trigger-based invalidation"
            with pytest.raises(ValueError, match="empty target"):
                await restore_logical_backup(target, path)
        finally:
            await target.dispose()
    raw = path.read_bytes()
    changed = raw.replace(b"sealed-fixture", b"changed-marker")
    assert changed != raw
    path.write_bytes(changed)
    with pytest.raises(RuntimeError, match="integrity"):
        verify_logical_backup(path)
