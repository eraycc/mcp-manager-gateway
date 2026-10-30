"""Bounded offline conversion of old catalog journals and per-revision snapshots."""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
from types import SimpleNamespace

from sqlalchemy import select, text

from .catalog import CAPABILITY_KEYS, Catalog
from .credentials import CredentialStore
from .database import McpServer, User
from .storage_async import run_blocking

MAX_RECORD_BYTES = 64 * 1024 * 1024


def _candidates(folder):
    for path in folder.iterdir():
        if path.name == "catalog.jsonl" or re.fullmatch(r".+-[0-9]+-(?:shared|[0-9a-f]{24})\.json", path.name):
            yield path


def _safe_file(folder, name):
    path = folder / name
    if path.name != name or path.is_symlink() or path.resolve().parent != folder.resolve():
        raise RuntimeError("Legacy cache path leaves the managed cache directory")
    return path


def _stage_sources(folder, index_path):
    """Retain only source offsets in SQLite; never replay a journal into a Python map."""
    with contextlib.closing(sqlite3.connect(index_path, timeout=5)) as connection:
        connection.execute("DELETE FROM legacy_catalog_import")
        for path in _candidates(folder):
            _safe_file(folder, path.name)
            with path.open("rb") as stream:
                offset = 0
                while True:
                    raw = stream.readline(MAX_RECORD_BYTES + 1) if path.suffix == ".jsonl" else stream.read(MAX_RECORD_BYTES + 1)
                    if not raw:
                        break
                    if len(raw) > MAX_RECORD_BYTES:
                        raise RuntimeError("Legacy catalog record exceeds migration byte budget")
                    position, offset = offset, offset + len(raw)
                    if not raw.strip():
                        continue
                    try:
                        value = json.loads(raw)
                        if path.suffix == ".json":
                            if not isinstance(value, dict):
                                raise ValueError
                            identity, revision, owner = path.stem.rsplit("-", 2)
                            key, revision, priority = identity + "-" + owner, int(revision), 1
                        else:
                            key, payload, priority = value["key"], value.get("value"), 2
                            revision = int(payload["revision"]) if isinstance(payload, dict) else -1
                        if not isinstance(key, str) or len(key) > 512:
                            raise ValueError
                    except (ValueError, KeyError, TypeError, UnicodeError):
                        raise RuntimeError("Legacy catalog evidence is unreadable; source preserved") from None
                    connection.execute("""INSERT INTO legacy_catalog_import VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(key) DO UPDATE SET revision=excluded.revision,source=excluded.source,
                        offset_bytes=excluded.offset_bytes,size_bytes=excluded.size_bytes,
                        sha256=excluded.sha256,priority=excluded.priority
                        WHERE excluded.priority>legacy_catalog_import.priority
                        OR (excluded.priority=legacy_catalog_import.priority AND
                            (excluded.priority=2 OR excluded.revision>=legacy_catalog_import.revision))""",
                        (key, revision, path.name, position, len(raw), hashlib.sha256(raw).hexdigest(), priority))
                    if path.suffix == ".json":
                        break
            connection.commit()


def _read_candidate(folder, candidate):
    path = _safe_file(folder, candidate.source)
    with path.open("rb") as stream:
        stream.seek(candidate.offset_bytes)
        raw = stream.read(candidate.size_bytes)
    if hashlib.sha256(raw).hexdigest() != candidate.sha256:
        raise RuntimeError("Legacy cache changed during offline import")
    value = json.loads(raw)
    return value.get("value") if path.suffix == ".jsonl" else {
        "revision": candidate.revision, "data": value}


async def _owners(database):
    async with database.derived.locked() as session:
        await session.execute(text("DELETE FROM storage_migration_owners"))
        await session.execute(text("INSERT INTO storage_migration_owners VALUES(:hash,:owner)"), [
            {"hash": hashlib.sha256(owner.encode()).hexdigest()[:24], "owner": owner}
            for owner in ("service", "anonymous")] + [{"hash": "shared", "owner": "service"}])
    previous = ""
    while True:
        async with database.session() as session:
            owners = list(await session.scalars(select(User.id).where(User.id > previous).order_by(User.id).limit(250)))
        if not owners:
            return
        previous = owners[-1]
        async with database.derived.locked() as session:
            await session.execute(text("INSERT OR IGNORE INTO storage_migration_owners VALUES(:hash,:owner)"), [
                {"hash": hashlib.sha256(owner.encode()).hexdigest()[:24], "owner": owner} for owner in owners])


async def import_legacy_catalog(database, archive_relative):
    folder = database.settings.data_dir / "cache"
    if not folder.is_dir() or not any(_candidates(folder)):
        return {"imported": 0, "unresolved": 0, "archive": None}
    archive = (database.settings.data_dir / archive_relative).resolve()
    if not archive.is_relative_to(database.settings.data_dir.resolve()) or archive == folder.resolve():
        raise ValueError("Invalid legacy cache archive")
    await run_blocking(_stage_sources, folder, database.derived.path)
    await _owners(database)
    runtime = SimpleNamespace(versions={})
    catalog = Catalog(database, runtime, None, database.settings)
    catalog.credentials = CredentialStore(catalog)
    imported, unresolved, previous = 0, 0, ""
    try:
        while True:
            async with database.derived.session() as session:
                candidates = (await session.execute(text(
                    "SELECT * FROM legacy_catalog_import WHERE key>:key ORDER BY key LIMIT 250"),
                    {"key": previous})).all()
            if not candidates:
                break
            previous = candidates[-1].key
            for candidate in candidates:
                if candidate.revision < 0:
                    continue  # A journal tombstone must never resurrect an older JSON file.
                identity, owner_hash = candidate.key.rsplit("-", 1)
                async with database.session() as session:
                    server = await session.get(McpServer, identity)
                async with database.derived.session() as session:
                    owner = await session.scalar(text("SELECT owner FROM storage_migration_owners WHERE hash=:hash"),
                                                 {"hash": owner_hash})
                if not owner or not server:
                    unresolved += 1
                    continue
                if server.revision != candidate.revision or server.mode == "disabled":
                    continue
                scope = await catalog.index_scope(server, owner if server.isolation == "user" else None)
                if scope.owner != owner:
                    unresolved += 1
                    continue
                # Indexed SQL generations are newer and take precedence over compatibility files.
                if (await catalog.index.get_summary(scope))["generation"] is not None:
                    continue
                payload = await run_blocking(_read_candidate, folder, candidate)
                data = payload.get("data", {}) if isinstance(payload, dict) else {}
                if data.get("cache_status") == "ready":
                    result = {kind: data.get(kind, []) for kind in CAPABILITY_KEYS}
                    result.update({key: data.get(key) for key in ("serverInfo", "protocolVersion", "capability_errors")})
                    await catalog.store_discovery(server, result, owner if server.isolation == "user" else None)
                    for kind in CAPABILITY_KEYS:
                        page = await catalog.index.list_capabilities(scope, kind=kind, limit=1)
                        if page.total != len(result[kind]):
                            raise RuntimeError("Imported legacy catalog count verification failed")
                    imported += 1
                else:
                    await run_blocking(catalog.save_cache, server, data, owner if server.isolation == "user" else None)
        if unresolved:
            # Unknown owner evidence stays available for explicit operator review.
            return {"imported": imported, "unresolved": unresolved, "archive": None}
        archive.mkdir(parents=True, exist_ok=True)
        def preserve():
            for path in _candidates(folder):
                _safe_file(folder, path.name)
                target = archive / path.name
                if target.exists():
                    raise RuntimeError("Legacy archive conflict; source preserved")
                os.replace(path, target)
        await run_blocking(preserve)
        async with database.derived.locked() as session:
            await session.execute(text("DELETE FROM legacy_catalog_import"))
            await session.execute(text("DELETE FROM storage_migration_owners"))
        return {"imported": imported, "unresolved": 0,
                "archive": archive.relative_to(database.settings.data_dir.resolve()).as_posix()}
    finally:
        await catalog.close()
