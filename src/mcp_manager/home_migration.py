"""Offline, non-destructive migration from a checkout data directory."""
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from dotenv import dotenv_values
from filelock import FileLock

from .config import normalize_database_url

ENV_EXAMPLE = """# MCP Manager user configuration; environment variables take precedence.
# All relative data paths are resolved below this user directory.
HOST=127.0.0.1
PORT=8765
PUBLIC_URL=http://127.0.0.1:8765
# DATA_DIR=.
# DATABASE_URL=mysql://user:password@localhost:3306/mcp_manager?charset=utf8mb4
# COOKIE_SECURE=false
"""


def initialize_home(home):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    try:
        with (home / ".env").open("x", encoding="utf-8") as file:
            file.write(ENV_EXAMPLE)
        (home / ".env").chmod(0o600)
    except FileExistsError:
        pass


def migrate_home(source, destination, source_env=None):
    source, destination = Path(source).expanduser().resolve(), Path(destination).expanduser().resolve()
    if (source == Path(source.anchor) or source == Path.home().resolve() or not source.is_dir()
            or source == destination or source in destination.parents or destination in source.parents):
        raise ValueError("Use distinct, non-nested application data directories")
    values = {}
    if source_env:
        source_env = Path(source_env).expanduser().resolve()
        if not source_env.is_file():
            raise ValueError("Source configuration file does not exist")
        try:
            values = dotenv_values(source_env, encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ValueError("Source configuration file is not readable") from exc
        fields = {"DATABASE_URL", "SECRET_KEY", "DATA_DIR", "HOST", "PORT", "PUBLIC_URL", "COOKIE_SECURE"}
        values = {(key.upper() if key.upper() in fields else key): value for key, value in values.items()}
    if not values.get("SECRET_KEY") and not (source / "secret.key").is_file():
        raise ValueError("Source secret.key or explicit SECRET_KEY is required to preserve existing credentials")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(source / "runtime.lock"), timeout=0), FileLock(str(destination) + ".migration.lock", timeout=0):
        if destination.exists() and any(destination.iterdir()):
            raise ValueError("Destination must be empty; existing user data will not be overwritten")
        staging = Path(tempfile.mkdtemp(prefix="." + destination.name + "-migration-", dir=destination.parent)).resolve()
        try:
            for name in ("secret.key", "cache", "logs", "indexes", "jobs"):
                item = source / name
                if not item.exists():
                    continue
                if item.is_symlink() or (item.is_dir() and any(p.is_symlink() for p in item.rglob("*"))):
                    raise ValueError("Migration refuses symlinks in application data")
                if item.is_dir():
                    shutil.copytree(item, staging / name)
                else:
                    shutil.copy2(item, staging / name)
            if values.get("SECRET_KEY"):
                (staging / "secret.key").write_text(values["SECRET_KEY"], encoding="utf-8")
            database_url = values.get("DATABASE_URL") or ""
            sqlite_path = source / "mcp-manager.sqlite"
            if database_url.startswith("sqlite"):
                normalized = normalize_database_url(database_url, Path(source_env).resolve().parent)
                sqlite_path = Path(normalized.removeprefix("sqlite+aiosqlite:///"))
            if not database_url or database_url.startswith("sqlite"):
                if not sqlite_path.is_file():
                    raise ValueError("Source SQLite database does not exist")
                with closing(sqlite3.connect(sqlite_path)) as old, closing(sqlite3.connect(staging / "mcp-manager.sqlite")) as new:
                    old.backup(new)
                    if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("SQLite integrity check failed")
                values["DATABASE_URL"] = "sqlite://mcp-manager.sqlite"
            values["DATA_DIR"] = "."
            # Preserve configured secrets/settings without exposing them in the migration result.
            def quote(value):
                return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
            (staging / ".env").write_text(ENV_EXAMPLE + "\n" + "\n".join(
                key + "=" + quote(value) for key, value in values.items() if value is not None) + "\n", encoding="utf-8")
            (staging / "secret.key").chmod(0o600)
            (staging / ".env").chmod(0o600)
            if destination.exists():
                destination.rmdir()  # exact verified empty directory only
            os.replace(staging, destination)
            return {"source": str(source), "destination": str(destination), "source_preserved": True}
        finally:
            if staging.exists() and staging.parent == destination.parent and staging.name.startswith(
                    "." + destination.name + "-migration-"):
                shutil.rmtree(staging)
