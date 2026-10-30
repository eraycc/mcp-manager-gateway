"""Resumable, offline migration from main cache tables into the single derived DB."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from filelock import BaseFileLock, FileLock, Timeout
from sqlalchemy import MetaData, func, inspect, select, tuple_
from sqlalchemy.dialects.sqlite import insert

from .config import PACKAGE_ROOT
from .db_types import strict_json_dumps
from .storage_async import run_blocking

LAYOUT_KEY = "_storage_layout"
FORMAT_VERSION = 2
CACHE_TABLES = ("artifact_objects", "catalog_generations", "catalog_heads",
                "catalog_entries", "catalog_terms", "embedding_vectors")
DROP_ORDER = ("embedding_vectors", "catalog_terms", "catalog_entries",
              "catalog_heads", "catalog_generations", "artifact_objects")
HEALTH_FIELDS = {
    "startup_failure_count", "runtime_status", "last_startup_error_code", "last_startup_error",
    "last_startup_failure_at", "last_refresh_error", "last_refresh_error_code",
    "last_refresh_failure_at", "cache_error", "cache_error_code", "cache_stale",
}


def portable(value):
    if isinstance(value, datetime):
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {key: portable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [portable(item) for item in value]
    return value


async def _names(db):
    async with db.engine.connect() as connection:
        return await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))


async def _receipt(db):
    from .database import SystemSetting

    async with db.session() as session:
        row = await session.get(SystemSetting, LAYOUT_KEY)
        return dict(row.value) if row else {}


async def _save(db, value):
    from .database import SystemSetting

    async with db.locked() as session:
        row = await session.get(SystemSetting, LAYOUT_KEY)
        if row:
            row.value = portable(value)
        else:
            session.add(SystemSetting(key=LAYOUT_KEY, value=portable(value)))


async def _upgrade(db, revision, **attributes):
    def upgrade(connection):
        cfg = Config()
        cfg.set_main_option("script_location", str(PACKAGE_ROOT / "migrations"))
        cfg.attributes.update(connection=connection, **attributes)
        command.upgrade(cfg, revision)

    async with db.engine.begin() as connection:
        await connection.run_sync(upgrade)


async def _reflect(db):
    metadata = MetaData()
    async with db.engine.connect() as connection:
        await connection.run_sync(lambda conn: metadata.reflect(conn))
    return metadata.tables


async def _batches(engine, selectable, *, where=None, keys=None, limit=250):
    keys = list(keys or selectable.primary_key.columns)
    previous = None
    while True:
        statement = select(selectable).order_by(*keys).limit(limit)
        if where is not None:
            statement = statement.where(where)
        if previous is not None:
            statement = statement.where(tuple_(*keys) > tuple_(*previous))
        async with engine.connect() as connection:
            rows = list((await connection.execute(statement)).mappings().all())
        if not rows:
            return
        previous = tuple(rows[-1][column.key] for column in keys)
        yield [dict(row) for row in rows]


def _selection(tables, name):
    head = tables["catalog_heads"]
    active = select(head.c.generation_id).where(head.c.generation_id.is_not(None))
    table = tables[name]
    if name == "catalog_generations":
        return table.c.id.in_(active)
    if name in {"catalog_entries", "catalog_terms", "embedding_vectors"}:
        return table.c.generation_id.in_(active)
    if name == "artifact_objects":
        entries, vectors = tables["catalog_entries"], tables["embedding_vectors"]
        refs = select(entries.c.artifact_id).where(entries.c.generation_id.in_(active)).union(
            select(vectors.c.artifact_id).where(vectors.c.generation_id.in_(active)))
        return table.c.id.in_(refs)
    return None


async def _backup(db, receipt, tables):
    root = db.settings.data_dir
    relative = Path("backups") / "storage" / receipt["run_id"]
    folder = root / relative
    folder.mkdir(parents=True, exist_ok=True)
    if db.engine.dialect.name == "sqlite":
        destination = folder / "main.sqlite"
        if not destination.exists():
            temporary = folder / "main.sqlite.tmp"
            source = db.engine.url.database
            if source and source != ":memory:":
                def copy():
                    with (contextlib.closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro",
                                                            uri=True)) as original,
                          contextlib.closing(sqlite3.connect(temporary)) as target):
                        original.backup(target)
                        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                            raise RuntimeError("Storage backup integrity verification failed")
                    os.replace(temporary, destination)
                await asyncio.to_thread(copy)
            else:
                async with db.engine.connect() as connection:
                    raw = await connection.get_raw_connection()
                    target = sqlite3.connect(temporary, check_same_thread=False)
                    try:
                        await raw.driver_connection.backup(target)
                    finally:
                        target.close()
                os.replace(temporary, destination)
        return (relative / "main.sqlite").as_posix()
    from .storage_backup import write_logical_backup
    destination = folder / "main.jsonl"
    evidence = await write_logical_backup(db.engine, tables, destination, run_id=receipt["run_id"])
    receipt["backup_sha256"] = evidence["sha256"]
    return (relative / "main.jsonl").as_posix()


async def _copy_legacy_tables(db, tables, receipt):
    from .catalog_models import CatalogEntry, CatalogGeneration
    from .catalog_terms import insert_term_rows
    from .derived_models import CatalogServer, DerivedBase

    server_table = tables["mcp_servers"]
    async for rows in _batches(db.engine, server_table):
        values = [{key: row[key] for key in ("id", "slug", "revision", "mode", "isolation")} for row in rows]
        async with db.derived.locked() as session:
            await session.execute(insert(CatalogServer).on_conflict_do_nothing(), values)
    for name in CACHE_TABLES:
        source = tables[name]
        target = DerivedBase.metadata.tables[name]
        async for rows in _batches(db.engine, source, where=_selection(tables, name)):
            async with db.derived.locked() as session:
                if name == "catalog_terms":
                    await insert_term_rows(session, rows)
                else:
                    await session.execute(insert(target).on_conflict_do_nothing(), rows)
            receipt["progress"] = {"table": name, "last_key": [
                portable(rows[-1][column.key]) for column in source.primary_key.columns]}
            await _save(db, receipt)
    # Ensure a ready source does not advertise rows which never existed.
    async with db.derived.session() as session:
        actual = {}
        for generation, kind, count in (await session.execute(select(
                CatalogEntry.generation_id, CatalogEntry.kind, func.count()).group_by(
                    CatalogEntry.generation_id, CatalogEntry.kind))).all():
            actual.setdefault(generation, {})[kind] = count
        generations = list((await session.scalars(select(CatalogGeneration))).all())
        for generation in generations:
            if generation.status == "ready" and any(
                    (generation.counts or {}).get(kind, 0) != actual.get(generation.id, {}).get(kind, 0)
                    for kind in ("tools", "resources", "prompts", "templates")):
                raise RuntimeError("Legacy catalog counts do not match stored definitions")
    return generations


async def _copy_object(db, identity, size, namespace):
    if not isinstance(identity, str) or not re.fullmatch("[0-9a-f]{64}", identity):
        raise RuntimeError("Invalid legacy artifact reference")
    if type(size) is not int or size < 0:
        raise RuntimeError("Invalid legacy artifact size")
    base = db.settings.data_dir / "objects"
    target = base / namespace / "sha256" / identity[:2] / identity[2:4] / (identity + ".json")
    candidates = [target, base / identity[:2] / (identity + ".json")]
    candidates += [base / role / "sha256" / identity[:2] / identity[2:4] / (identity + ".json")
                   for role in ("cache", "durable")]

    def copy():
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            raise RuntimeError("Referenced artifact is missing; source tables were preserved")
        if not source.resolve().is_relative_to(base.resolve()) or source.is_symlink():
            raise RuntimeError("Artifact reference leaves the managed object directory")
        digest, count = hashlib.sha256(), 0
        with source.open("rb") as stream:
            while raw := stream.read(65536):
                digest.update(raw)
                count += len(raw)
        if count != size or digest.hexdigest() != identity:
            raise RuntimeError("Legacy artifact integrity verification failed")
        if source == target:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / ("." + uuid4().hex + ".tmp")
        try:
            with source.open("rb") as stream, temporary.open("xb") as output:
                shutil.copyfileobj(stream, output, length=65536)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    await asyncio.to_thread(copy)


async def _copy_objects(db, tables):
    from .catalog_types import ArtifactRef
    from .object_inventory import ObjectInventory
    inventory = await asyncio.to_thread(ObjectInventory, db.settings.data_dir)
    for name in ("job_results", "proposal_test_entries", "catalog_entries", "embedding_vectors"):
        table = tables[name]
        where = _selection(tables, name) if name in {"catalog_entries", "embedding_vectors"} else None
        namespace = "cache" if name in {"catalog_entries", "embedding_vectors"} else "durable"
        async for rows in _batches(db.engine, table, where=where):
            for row in rows:
                await _copy_object(db, row["artifact_id"], row["artifact_bytes"], namespace)
            await asyncio.to_thread(inventory.register_many, [
                ArtifactRef(row["artifact_id"], row["artifact_bytes"], row["artifact_id"], namespace) for row in rows])
    for namespace in ("cache", "durable"):
        while not (await asyncio.to_thread(inventory.scan_next, namespace))["complete"]:
            await asyncio.sleep(0)
        await asyncio.to_thread(inventory.finish_roots, namespace)


def _target_query(name):
    from .catalog_models import CatalogEntry, CatalogGeneration, CatalogTerm, CatalogWord
    from .derived_models import DerivedBase

    if name != "catalog_terms":
        return DerivedBase.metadata.tables[name]
    return select(
        CatalogGeneration.id.label("generation_id"), CatalogEntry.id.label("entry_id"),
        CatalogWord.term_hash.label("term_hash"), CatalogWord.term.label("term"),
        CatalogTerm.frequency.label("frequency"),
    ).select_from(CatalogTerm).join(CatalogGeneration, CatalogGeneration.index_id == CatalogTerm.generation_id
        ).join(CatalogEntry, CatalogEntry.index_id == CatalogTerm.entry_id
        ).join(CatalogWord, CatalogWord.id == CatalogTerm.term_id).subquery()


async def _digest(engine, selectable, columns, *, where=None, keys=None):
    accumulator, count = hashlib.sha256(), 0
    async for rows in _batches(engine, selectable, where=where, keys=keys):
        for row in rows:
            accumulator.update(strict_json_dumps({key: portable(row[key]) for key in columns}).encode())
            accumulator.update(b"\n")
            count += 1
    return count, accumulator.hexdigest()


async def _verify(db, tables):
    verified = {}
    for name in CACHE_TABLES:
        source, target = tables[name], _target_query(name)
        columns = [column.name for column in source.columns]
        keys = [target.c[column.name] for column in source.primary_key.columns]
        source_value = await _digest(db.engine, source, columns, where=_selection(tables, name))
        target_value = await _digest(db.derived.engine, target, columns, keys=keys)
        if source_value != target_value:
            raise RuntimeError("Storage migration verification failed for " + name)
        verified[name] = {"rows": source_value[0], "sha256": source_value[1]}
    return verified


async def _controls(db, receipt):
    """Spool bounded control-only evidence; never put all scopes in the main receipt."""
    from sqlalchemy import text

    from .catalog_models import CatalogGeneration, CatalogHead
    from .catalog_store import scope_key
    from .catalog_types import CatalogScope
    from .database import McpServer, SystemSetting, User

    async with db.derived.locked() as session:
        await session.execute(text("DELETE FROM storage_migration_health"))
        await session.execute(text("DELETE FROM storage_migration_owners"))
    owners = ["service", "anonymous"]
    async for rows in _batches(db.engine, User.__table__):
        owners.extend(row["id"] for row in rows)
        async with db.derived.locked() as session:
            await session.execute(text("INSERT INTO storage_migration_owners(hash,owner) VALUES(:hash,:owner)"),
                [{"hash": hashlib.sha256(owner.encode()).hexdigest()[:24], "owner": owner} for owner in owners])
        owners.clear()
    if owners:
        async with db.derived.locked() as session:
            await session.execute(text("INSERT INTO storage_migration_owners(hash,owner) VALUES(:hash,:owner)"),
                [{"hash": hashlib.sha256(owner.encode()).hexdigest()[:24], "owner": owner} for owner in owners])
    path = db.settings.data_dir / "cache" / "catalog.jsonl"
    if path.exists():
        with path.open("rb") as stream:
            while raw := stream.readline(64 * 1024 * 1024 + 1):
                if len(raw) > 64 * 1024 * 1024:
                    raise RuntimeError("Legacy cache record exceeds migration safety budget")
                if not raw.strip():
                    continue
                try:
                    item = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise RuntimeError("Legacy cache has incomplete control evidence") from None
                key, value = item.get("key"), item.get("value")
                if not isinstance(key, str):
                    raise RuntimeError("Invalid legacy cache key")  # noqa: TRY004 - corrupt persisted evidence, not caller type
                async with db.derived.locked() as session:
                    if value is None:
                        await session.execute(text("DELETE FROM storage_migration_health WHERE key=:key"), {"key": key})
                    elif isinstance(value, dict):
                        data = value.get("data", {})
                        health = {field: data[field] for field in HEALTH_FIELDS if field in data}
                        await session.execute(text("""INSERT INTO storage_migration_health VALUES(:key,:revision,:health)
                            ON CONFLICT(key) DO UPDATE SET revision=excluded.revision,health=excluded.health"""),
                            {"key": key, "revision": value.get("revision"), "health": strict_json_dumps(health)})
    folder = db.settings.data_dir / "backups" / "storage" / receipt["run_id"]
    folder.mkdir(parents=True, exist_ok=True)
    destination, temporary = folder / "controls.jsonl", folder / "controls.jsonl.tmp"
    stamp = datetime.now(UTC).isoformat()

    def control(scope, health):
        return {"scope_key": scope_key(scope), "server_id": scope.server_id, "owner": scope.owner,
                "revision": scope.revision, "credential_generation": scope.credential_generation,
                "epoch": 0, "refresh_fence": 0, "result_sequence": 0, "success_sequence": 0,
                "receipt": {}, "health": health, "updated_at": stamp}

    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        current = select(CatalogHead.generation_id).where(CatalogHead.generation_id.is_not(None))
        async for rows in _batches(db.derived.engine, CatalogGeneration.__table__,
                                   where=CatalogGeneration.id.in_(current)):
            for row in rows:
                cache_key = row["server_id"] + "-" + hashlib.sha256(row["owner"].encode()).hexdigest()[:24]
                async with db.derived.locked() as session:
                    health_row = (await session.execute(text(
                        "SELECT revision,health FROM storage_migration_health WHERE key=:key"), {"key": cache_key})).first()
                    health = json.loads(health_row.health) if health_row and health_row.revision == row["revision"] else {}
                    await session.execute(text("DELETE FROM storage_migration_health WHERE key=:key"), {"key": cache_key})
                scope = CatalogScope(row["server_id"], row["owner"], row["revision"], row["credential_generation"])
                output.write(strict_json_dumps(control(scope, health)) + "\n")
        previous = ""
        while True:
            async with db.derived.session() as session:
                rows = (await session.execute(text("""SELECT h.key,h.revision,h.health,o.owner
                    FROM storage_migration_health h JOIN storage_migration_owners o
                    ON o.hash=substr(h.key,-24) WHERE h.key>:previous ORDER BY h.key LIMIT 250"""),
                    {"previous": previous})).all()
            if not rows:
                break
            previous = rows[-1].key
            for row in rows:
                server_id = row.key.rsplit("-", 1)[0]
                async with db.session() as primary:
                    server = await primary.get(McpServer, server_id)
                    if not server or server.revision != row.revision:
                        continue
                    if server.isolation != "user" and row.owner != "service":
                        continue
                    keys = sorted({f"credential:{server_id}:{row.owner}", f"credential:{server_id}:service",
                                   f"oauth:{server_id}:{row.owner}"})
                    stored = dict((await primary.execute(select(SystemSetting.key, SystemSetting.value).where(
                        SystemSetting.key.in_(keys)))).all())
                    fingerprint = hashlib.sha256(strict_json_dumps(
                        [server.revision, [[key, stored.get(key)] for key in keys]]).encode()).hexdigest()
                scope = CatalogScope(server_id, row.owner, row.revision, fingerprint)
                output.write(strict_json_dumps(control(scope, json.loads(row.health))) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, destination)
    digest = hashlib.sha256()
    with destination.open("rb") as stream:
        while raw := stream.read(65536):
            digest.update(raw)
    return {"path": destination.relative_to(db.settings.data_dir).as_posix(), "sha256": digest.hexdigest()}


async def initialize_storage(db, *, allow_migration=None, runtime_guard=None):
    from .derived_index import DerivedIndex
    from .derived_models import DerivedBase

    settings = db.settings
    lock = FileLock(str(settings.data_dir / "storage-upgrade.lock"), timeout=0, thread_local=False)
    receipt, owned_runtime, may_mutate = {}, None, False
    try:
        deadline = time.monotonic() + 600
        while True:
            try:
                await run_blocking(lock.acquire, timeout=0)
                break
            except Timeout:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Storage upgrade is still running; inspect storage status before retrying") from None
                await asyncio.sleep(.1)
        names = await _names(db)
        fresh_install = not names
        receipt = await _receipt(db) if "system_settings" in names else {}
        enabled = settings.storage_migration_mode == "auto" if allow_migration is None else allow_migration
        if receipt.get("phase") == "completed":
            if set(CACHE_TABLES) & names:
                raise RuntimeError("Storage migration marker conflicts with the main schema")
        elif not enabled and not fresh_install:
            raise RuntimeError("Storage migration required; run 'mmg storage migrate' in maintenance mode")
        if receipt.get("phase") != "completed":
            runtime_path = (settings.data_dir / "runtime.lock").resolve()
            if runtime_guard is not None:
                if (not isinstance(runtime_guard, BaseFileLock) or not runtime_guard.is_locked
                        or Path(runtime_guard.lock_file).resolve() != runtime_path):
                    raise RuntimeError("Storage migration requires an active runtime ownership guard")
            else:
                owned_runtime = FileLock(str(runtime_path), timeout=0, thread_local=False)
                try:
                    await run_blocking(owned_runtime.acquire, timeout=0)
                except Timeout:
                    raise RuntimeError("Stop all runtime writers before storage migration or pass the maintenance guard") from None
            may_mutate = True
        # Older application releases may predate 0010. Never downgrade an
        # already cut-over database when resuming a partial MySQL DDL sequence.
        async with db.engine.connect() as connection:
            from sqlalchemy import text
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version")) if "alembic_version" in names else None
        if revision is None or revision < "0010":
            await _upgrade(db, "0010")
            names = await _names(db)
        has_data = not fresh_install
        if db.derived is None:
            db.derived = DerivedIndex(db)
        await db.derived.initialize()
        db.session.configure(binds={table: db.derived.engine for table in DerivedBase.metadata.tables.values()})
        if receipt.get("phase") == "completed":
            # Future ordinary authority migrations still run after storage cutover.
            await _upgrade(db, "head", storage_migration_verified=True)
            return receipt
        if not receipt:
            receipt = {"version": FORMAT_VERSION, "phase": "pending", "run_id": uuid4().hex}
            await _save(db, receipt)
        phase = receipt.get("resume_phase", receipt["phase"]) if receipt["phase"] == "failed" else receipt["phase"]
        if phase not in {"validated", "schema_cutover"}:
            tables = await _reflect(db)
            if not set(CACHE_TABLES) <= set(tables):
                raise RuntimeError("Incomplete legacy schema without a validated migration receipt")
            if not receipt.get("backup") and has_data:
                receipt["backup"] = await _backup(db, receipt, tables)
            receipt.update(phase="prepared")
            await _save(db, receipt)
            await _copy_legacy_tables(db, tables, receipt)
            await _copy_objects(db, tables)
            receipt.update(phase="copied")
            await _save(db, receipt)
            receipt["verified"] = await _verify(db, tables)
            receipt["controls"] = await _controls(db, receipt)
            receipt.update(phase="validated")
            await _save(db, receipt)
        # Do not store per-scope control payloads indefinitely in the layout row.
        controls = receipt.get("controls", {})
        receipt.update(phase="schema_cutover")
        await _save(db, receipt)
        await _upgrade(db, "head", storage_migration_verified=True, storage_controls=controls,
                       storage_data_dir=db.settings.data_dir)
        from .legacy_catalog_import import import_legacy_catalog
        receipt["legacy_cache"] = await import_legacy_catalog(
            db, Path("backups") / "storage" / receipt["run_id"] / "legacy-cache")
        receipt.pop("controls", None)
        receipt.pop("progress", None)
        receipt.pop("resume_phase", None)
        receipt.pop("error", None)
        if set(CACHE_TABLES) & await _names(db):
            raise RuntimeError("Storage schema cutover did not remove all legacy cache tables")
        receipt.update(phase="completed", completed_at=datetime.now(UTC).isoformat())
        await _save(db, receipt)
        return receipt
    except BaseException as exc:
        if may_mutate and receipt and receipt.get("phase") != "completed":
            previous = receipt.get("resume_phase", receipt.get("phase", "pending"))
            receipt.update(phase="failed", resume_phase=previous, error=type(exc).__name__)
            with contextlib.suppress(Exception):
                await asyncio.shield(_save(db, receipt))
        raise
    finally:
        if owned_runtime is not None and owned_runtime.is_locked:
            await run_blocking(owned_runtime.release)
        if lock.is_locked:
            await run_blocking(lock.release)


async def storage_status(db):
    """Inspect migration evidence without calling the migration entry point."""
    names = await _names(db)
    value = await _receipt(db) if "system_settings" in names else {}
    tables = await _reflect(db)
    counts = {}
    async with db.engine.connect() as connection:
        for name in CACHE_TABLES:
            if name in tables:
                counts[name] = await connection.scalar(select(func.count()).select_from(tables[name]))
    return {"format": value.get("version", 1 if names else None),
            "phase": value.get("phase", "required" if names else "fresh"),
            "source_cache_rows": counts,
            "backup": value.get("backup"), "verified": value.get("verified", {}),
            "index_path": "indexes/derived.sqlite", "call_path": "logs/calls/YYYY-MM-DD.jsonl"}
