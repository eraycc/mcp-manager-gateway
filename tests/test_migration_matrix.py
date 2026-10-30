"""Catch data loss and dialect failures in an upgrade from the existing release."""
from io import StringIO

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from mcp_manager.config import PACKAGE_ROOT
from mcp_manager.database import McpProposal


def migration_config():
    cfg = Config()
    cfg.set_main_option("script_location", str(PACKAGE_ROOT / "migrations"))
    return cfg


async def test_upgrade_preserves_legacy_identity_and_payload(db_settings):
    from mcp_manager.database import Database
    db = Database(db_settings)
    try:
        assert hasattr(McpProposal, "revision"), "Proposal edits and tests need a persisted revision"
        def legacy(connection):
            cfg = migration_config()
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "0006")
        async with db.engine.begin() as connection:
            await connection.run_sync(legacy)
            await connection.execute(text(
                "INSERT INTO users (id,username,email,password_hash,role,disabled,auth_version,"
                "scope_mode,mcp_ids,created_at) VALUES "
                "('legacy-user','legacy','','synthetic','admin',0,1,'all','[]','2026-10-13 00:00:00')"
            ))
            await connection.execute(text(
                "INSERT INTO api_tokens (id,user_id,name,prefix,token_hash,disabled,scope_mode,mcp_ids,"
                "discovery_mode,enable_resource_tools,enable_mcp_proposal,created_at) VALUES "
                "('legacy-token','legacy-user','legacy','test','hash',0,'all','[]','discovery',0,1,"
                "'2026-10-13 00:00:00')"
            ))
            await connection.execute(text(
                "INSERT INTO mcp_proposals (id,user_id,token_id,source,purpose,declared_capabilities,"
                "requested_permissions,payload,status,rejection_reason,created_at,updated_at) VALUES "
                "('legacy-proposal','legacy-user','legacy-token','legacy','','[]','[]',"
                "'{\"sealed\":\"unchanged-marker\"}','pending','','2026-10-13 00:00:00','2026-10-13 00:00:00')"
            ))
        await db.initialize()
        async with db.session() as session:
            row = await session.get(McpProposal, "legacy-proposal")
            assert row.payload == {"sealed": "unchanged-marker"}
            assert row.revision == 1 and row.test_run_id is None and row.public_name is None
            head = ScriptDirectory.from_config(migration_config()).get_current_head()
            assert (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == head
        async with db.engine.connect() as connection:
            foreign_keys = await connection.run_sync(lambda conn: inspect(conn).get_foreign_keys("mcp_proposals"))
            assert {fk["referred_table"] for fk in foreign_keys} == {"users", "api_tokens", "mcp_servers"}
    finally:
        await db.close()


def test_offline_sql_supports_both_dialects(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for url in ("sqlite:///offline.db", "mysql+asyncmy://example:placeholder@localhost/mmg_test_offline"):
        monkeypatch.setenv("DATABASE_URL", url)
        output = StringIO()
        cfg = Config(output_buffer=output)
        cfg.set_main_option("script_location", str(PACKAGE_ROOT / "migrations"))
        command.upgrade(cfg, "0010", sql=True)
        sql = output.getvalue()
        assert "test_run_id" in sql and "tested_revision" in sql
