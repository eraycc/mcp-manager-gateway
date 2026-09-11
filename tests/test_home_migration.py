import sqlite3
from pathlib import Path

import pytest
from filelock import FileLock

from mcp_manager.home_migration import migrate_home


def test_home_copy_preserves_database_key_and_cache_without_deleting_source(tmp_path):
    source, target = tmp_path / "old", tmp_path / "new"
    source.mkdir()
    (source / "secret.key").write_text("stable-key", encoding="utf-8")
    (source / "cache").mkdir()
    (source / "cache/one.json").write_text('{"tools":[]}', encoding="utf-8")
    with sqlite3.connect(source / "mcp-manager.sqlite") as db:
        db.execute("create table example (value text)")
        db.execute("insert into example values ('saved')")
    result = migrate_home(source, target)
    assert result["destination"] == str(target)
    assert (target / "secret.key").read_text() == "stable-key"
    assert (target / ".env").exists()
    with sqlite3.connect(target / "mcp-manager.sqlite") as db:
        assert db.execute("select value from example").fetchone()[0] == "saved"
    assert (target / "cache/one.json").is_file()
    assert (source / "mcp-manager.sqlite").exists()
    with pytest.raises(ValueError, match="empty"):
        migrate_home(source, target)


def test_migration_refuses_running_source_and_missing_key(tmp_path):
    source, target = tmp_path / "old", tmp_path / "new"
    source.mkdir()
    (source / "secret.key").write_text("key")
    with FileLock(str(source / "runtime.lock")):
        with pytest.raises(Exception):
            migrate_home(source, target)
    assert not target.exists()
    (source / "secret.key").unlink()
    with pytest.raises(ValueError, match="secret.key"):
        migrate_home(source, target)
    assert not target.exists()


def test_explicit_env_secret_and_lowercase_mysql_config_are_preserved(tmp_path):
    source, target = tmp_path / "old", tmp_path / "new"
    source.mkdir()
    env = tmp_path / "source.env"
    env.write_text("secret_key=explicit-key\ndatabase_url=mysql://user:pw@host/db\n", encoding="utf-8")
    migrate_home(source, target, env)
    from mcp_manager.config import Settings
    settings = Settings(home_dir=target)
    assert settings.secret_key == "explicit-key"
    assert settings.database_url == "mysql+asyncmy://user:pw@host/db"
    assert (target / "secret.key").read_text() == "explicit-key"


def test_explicit_missing_env_file_never_falls_back_to_stale_data(tmp_path):
    source = tmp_path / "old"
    source.mkdir()
    (source / "secret.key").write_text("old-key")
    with pytest.raises(ValueError, match="configuration"):
        migrate_home(source, tmp_path / "new", tmp_path / "missing.env")
    assert not (tmp_path / "new").exists()


def test_reject_nested_or_root_source(tmp_path):
    with pytest.raises(ValueError):
        migrate_home(tmp_path, tmp_path / "nested")
    with pytest.raises(ValueError):
        migrate_home(Path(tmp_path.anchor), tmp_path / "new")
