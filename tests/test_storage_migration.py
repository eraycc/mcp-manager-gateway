"""One-time storage upgrade uses real old schemas and preserves recovery evidence."""
import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, select

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.catalog_store import CatalogStore, scope_key
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.config import PACKAGE_ROOT, Settings
from mcp_manager.database import Database, McpServer, SystemSetting
from mcp_manager.job_models import JobRecord, JobResult
from mcp_manager.job_store import JobStore

CACHE_TABLES = {"artifact_objects", "catalog_heads", "catalog_generations",
                "catalog_entries", "catalog_terms", "embedding_vectors"}


async def legacy_store(settings):
    """Seed 0010 directly; do not call the new auto migration entry point."""
    database = Database(settings)
    definition = {"name": "weather", "description": "weather", "inputSchema": {"type": "object"}}
    result = {"item": "legacy", "ok": True, "result": {"value": "irreplaceable"}}
    refs = []
    for value in (definition, result):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        identity = hashlib.sha256(raw).hexdigest()
        path = settings.data_dir / "objects" / identity[:2] / (identity + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        refs.append((identity, len(raw)))
    scope = CatalogScope("legacy-server", "service", 1, "shared")
    stamp = datetime.now(UTC).replace(tzinfo=None)
    generation, entry = "legacy-generation", "legacy-entry"

    def seed(connection):
        cfg = Config()
        cfg.set_main_option("script_location", str(PACKAGE_ROOT / "migrations"))
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "0010")
        metadata = MetaData()
        names = CACHE_TABLES | {"mcp_servers", "jobs", "job_results"}
        tables = {name: Table(name, metadata, autoload_with=connection) for name in names}
        connection.execute(McpServer.__table__.insert(), {
            "id": scope.server_id, "name": "Legacy", "slug": "legacy", "transport": "stdio", "config": {},
        })
        connection.execute(tables["artifact_objects"].insert(), [
            {"id": identity, "sha256": identity, "size_bytes": size, "created_at": stamp}
            for identity, size in refs
        ])
        connection.execute(tables["catalog_generations"].insert(), {
            "id": generation, "scope_key": scope_key(scope), "server_id": scope.server_id,
            "owner": "service", "revision": 1, "credential_generation": "shared", "status": "ready",
            "digest": "legacy-digest", "counts": {"tools": 1}, "details": {},
            "created_at": stamp, "published_at": stamp,
        })
        connection.execute(tables["catalog_heads"].insert(), {
            "scope_key": scope_key(scope), "server_id": scope.server_id, "owner": "service",
            "generation_id": generation, "refresh_id": generation, "status": "ready",
        })
        connection.execute(tables["catalog_entries"].insert(), {
            "id": entry, "generation_id": generation, "kind": "tools", "original_name": "weather",
            "name_hash": hashlib.sha256(b"weather").hexdigest(), "sort_key": b"weather".hex(),
            "gateway_name": "legacy__weather", "gateway_hash": hashlib.sha256(b"legacy__weather").hexdigest(),
            "legacy_gateway_hash": hashlib.sha256(b"legacy-server__weather").hexdigest(),
            "schema_hash": "", "summary": {"name": "weather"}, "search_text": "weather",
            "artifact_id": refs[0][0], "artifact_bytes": refs[0][1],
        })
        connection.execute(tables["catalog_terms"].insert(), {
            "generation_id": generation, "entry_id": entry,
            "term_hash": hashlib.sha256(b"weather").hexdigest(), "term": "weather", "frequency": 1,
        })
        connection.execute(JobRecord.__table__.insert(), {
            "id": "legacy-job", "action": "legacy", "status": "completed", "total": 1, "completed": 1,
        })
        connection.execute(JobResult.__table__.insert(), {
            "job_id": "legacy-job", "ordinal": 0, "summary": {"ok": True},
            "artifact_id": refs[1][0], "artifact_bytes": refs[1][1],
        })

    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(seed)
    finally:
        await database.close()
    return scope, definition, refs


async def table_names(database):
    async with database.engine.connect() as connection:
        return await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))


async def test_legacy_upgrade_preserves_head_and_durable_results_and_drops_only_cache(db_settings):
    assert importlib.util.find_spec("mcp_manager.storage_migration"), "Need one-time storage migration"
    scope, definition, _ = await legacy_store(db_settings)
    database = Database(db_settings)
    try:
        await database.initialize()
        assert CACHE_TABLES.isdisjoint(await table_names(database))
        store = CatalogStore(database, ArtifactStore(db_settings.data_dir), signing_key=db_settings.jwt_secret)
        assert await store.get_definition(scope, kind="tools", name="weather") == definition
        jobs = JobStore(database, store.artifacts)
        assert (await jobs.get_result("legacy-job", 0))["result"]["value"] == "irreplaceable"
        async with database.session() as session:
            receipt = (await session.get(SystemSetting, "_storage_layout")).value
        assert receipt["phase"] == "completed"
        backup = db_settings.data_dir / receipt["backup"]
        assert backup.is_file()
        if database.engine.dialect.name == "sqlite":
            with sqlite3.connect(backup) as connection:
                assert connection.execute("SELECT count(*) FROM job_results").fetchone()[0] == 1
                assert connection.execute("SELECT count(*) FROM catalog_entries").fetchone()[0] == 1
        else:
            records = [json.loads(line) for line in backup.read_text(encoding="utf-8").splitlines()]
            assert sum(item.get("table") == "job_results" for item in records) == 1
            assert sum(item.get("table") == "catalog_entries" for item in records) == 1
    finally:
        await database.close()


async def test_manual_mode_requires_command_and_completed_restart_skips_copy(db_settings, monkeypatch):
    assert importlib.util.find_spec("mcp_manager.storage_migration"), "Need one-time storage migration"
    from mcp_manager import storage_migration

    await legacy_store(db_settings)
    manual = Settings(data_dir=db_settings.data_dir, database_url=db_settings.database_url,
                      secret_key=db_settings.secret_key, storage_migration_mode="manual")
    database = Database(manual)
    try:
        with pytest.raises(RuntimeError, match="migration"):
            await database.initialize()
        assert CACHE_TABLES <= await table_names(database)
        await database.initialize(allow_storage_migration=True)
    finally:
        await database.close()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Completed migrations must not copy again")

    monkeypatch.setattr(storage_migration, "_copy_legacy_tables", forbidden)
    database = Database(manual)
    try:
        await database.initialize()
        assert CACHE_TABLES.isdisjoint(await table_names(database))
        async with database.session() as session:
            assert await session.get(JobRecord, "legacy-job")
    finally:
        await database.close()


async def test_copy_failure_keeps_source_and_resume_does_not_duplicate(db_settings, monkeypatch):
    assert importlib.util.find_spec("mcp_manager.storage_migration"), "Need one-time storage migration"
    from mcp_manager import storage_migration

    await legacy_store(db_settings)
    original = storage_migration._copy_legacy_tables

    async def injected(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected copy failure")

    monkeypatch.setattr(storage_migration, "_copy_legacy_tables", injected)
    database = Database(db_settings)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            await database.initialize()
        assert CACHE_TABLES <= await table_names(database)
    finally:
        await database.close()
    monkeypatch.setattr(storage_migration, "_copy_legacy_tables", original)
    database = Database(db_settings)
    try:
        await database.initialize()
        assert CACHE_TABLES.isdisjoint(await table_names(database))
        from mcp_manager.catalog_models import CatalogEntry, CatalogTerm
        async with database.derived.session() as session:
            assert len(list((await session.scalars(select(CatalogEntry))).all())) == 1
            assert len(list((await session.scalars(select(CatalogTerm))).all())) == 1
    finally:
        await database.close()


async def test_legacy_failure_control_survives_migration_and_blank_journal_lines(db_settings):
    scope, _, _ = await legacy_store(db_settings)
    legacy = db_settings.data_dir / "cache/catalog.jsonl"
    legacy.parent.mkdir(exist_ok=True)
    key = scope.server_id + "-" + hashlib.sha256(b"service").hexdigest()[:24]
    legacy.write_text("\n" + json.dumps({"key": key, "value": {
        "revision": 1, "data": {"startup_failure_count": 2, "cache_stale": True,
                                "tools": [{"name": "must-not-be-primary-data"}]}}}) + "\n",
        encoding="utf-8")
    database = Database(db_settings)
    try:
        await database.initialize()
        from mcp_manager.catalog_control import CatalogControl
        async with database.session() as session:
            control = await session.get(CatalogControl, scope_key(scope))
            assert control.health["startup_failure_count"] == 2
            assert "tools" not in control.health
    finally:
        await database.close()
