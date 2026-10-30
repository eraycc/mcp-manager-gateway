"""Older local snapshots migrate offline without starting a downstream MCP."""
import hashlib
import json

from cryptography.fernet import Fernet
from sqlalchemy import select
from test_storage_migration import legacy_store

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.catalog_control import CatalogControl
from mcp_manager.catalog_store import CatalogStore
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.database import Database, McpServer, SystemSetting


async def test_first_upgrade_imports_latest_json_snapshot_and_archives_sources(db_settings):
    await legacy_store(db_settings)
    database = Database(db_settings)
    owner = hashlib.sha256(b"service").hexdigest()[:24]
    folder = db_settings.data_dir / "cache"
    folder.mkdir(exist_ok=True)
    for revision, name in [(2, "current"), (1, "obsolete")]:
        (folder / f"snapshot-server-{revision}-{owner}.json").write_text(
            json.dumps({"tools": [{"name": name, "inputSchema": {"type": "object"}}],
                        "cache_status": "ready"}), encoding="utf-8")
    try:
        sealed = {"sealed": Fernet(db_settings.encryption_key).encrypt(b'{"command":"fake"}').decode()}
        async with database.engine.begin() as connection:
            await connection.execute(McpServer.__table__.insert(), {
                "id": "snapshot-server", "name": "snapshot", "slug": "snapshot",
                "revision": 2, "transport": "stdio", "config": sealed,
            })
        await database.initialize()
        store = ArtifactStore(db_settings.data_dir)
        try:
            index = CatalogStore(database, store, signing_key=db_settings.jwt_secret)
            async with database.session() as session:
                control = await session.scalar(select(CatalogControl).where(CatalogControl.server_id == "snapshot-server"))
            scope = CatalogScope("snapshot-server", "service", 2, control.credential_generation)
            assert (await index.get_definition(scope, kind="tools", name="current"))["name"] == "current"
            assert await index.get_definition(scope, kind="tools", name="obsolete") is None
            assert not list(folder.glob("*.json"))
            async with database.session() as session:
                receipt = (await session.get(SystemSetting, "_storage_layout")).value
            archive = db_settings.data_dir / receipt["legacy_cache"]["archive"]
            assert len(list(archive.glob("*.json"))) == 2
            assert receipt["legacy_cache"]["imported"] == 1
        finally:
            store.close()
    finally:
        await database.close()
