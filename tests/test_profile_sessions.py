"""Profile session history, deletion, and migration regression coverage."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from io import StringIO

import httpx
import pytest
import test_identity
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from test_identity import login, signup

from mcp_manager.config import PACKAGE_ROOT, Settings
from mcp_manager.database import AuthSession, Database, now

clients = test_identity.clients


async def test_login_records_peer_address_and_bounded_user_agent(clients):
    a, _, _ = clients
    await signup(a, "admin")
    a.headers.update({
        "User-Agent": "Browser/" + "x" * 1500,
        "X-Forwarded-For": "198.51.100.123",
        "X-Real-IP": "198.51.100.124",
    })
    await login(a, "admin")
    row = (await a.get("/api/v1/me/sessions")).json()[0]
    assert row["ip_address"] == "127.0.0.1"
    assert row["user_agent"] == "Browser/" + "x" * 1016
    assert row["active"] is True
    assert row["current"] is True
    assert "auth_version" not in row


async def test_login_without_peer_or_user_agent_keeps_unknown_metadata(clients):
    a, _, _ = clients
    await signup(a, "admin")
    app = a._transport.app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=None), base_url="http://test"
    ) as peerless:
        del peerless.headers["User-Agent"]
        await login(peerless, "admin")
        row = (await peerless.get("/api/v1/me/sessions")).json()[0]
    assert row["ip_address"] is None
    assert row["user_agent"] is None


@pytest.mark.parametrize("state", ["active", "revoked", "expired", "password_changed"])
async def test_permanent_delete_removes_noncurrent_session_and_invalidates_cookie(clients, state):
    a, b, db = clients
    await signup(a, "admin")
    await login(a, "admin")
    await login(b, "admin")
    row = next(row for row in (await a.get("/api/v1/me/sessions")).json() if not row["current"])
    if state != "active":
        async with db.locked() as session:
            target = await session.get(AuthSession, row["id"])
            if state == "revoked":
                target.revoked = True
            elif state == "expired":
                target.expires_at = now() - timedelta(minutes=1)
            else:
                target.auth_version = 0
    rows = (await a.get("/api/v1/me/sessions")).json()
    other = next(item for item in rows if item["id"] == row["id"])
    assert other["active"] is (state == "active")
    response = await a.delete("/api/v1/me/sessions/" + row["id"] + "/permanent")
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert [item["current"] for item in (await a.get("/api/v1/me/sessions")).json()] == [True]
    async with db.session() as session:
        assert await session.get(AuthSession, row["id"]) is None
    assert (await b.get("/api/v1/me")).status_code == 401


async def test_permanent_delete_rejects_current_foreign_missing_and_csrf(clients):
    a, b, db = clients
    await signup(a, "admin")
    await signup(b, "other")
    await login(a, "admin")
    await login(b, "other")
    own = (await a.get("/api/v1/me/sessions")).json()[0]
    foreign = (await b.get("/api/v1/me/sessions")).json()[0]
    base = "/api/v1/me/sessions/"
    assert (await a.delete(base + own["id"] + "/permanent")).status_code == 409
    assert (await a.delete(base + foreign["id"] + "/permanent")).status_code == 404
    assert (await a.delete(base + "missing/permanent")).status_code == 404
    assert (await a.delete(base + own["id"] + "/permanent",
                           headers={"X-CSRF-Token": ""})).status_code == 403
    assert (await a.get("/api/v1/me")).status_code == 200
    assert (await b.get("/api/v1/me")).status_code == 200
    async with db.session() as session:
        assert await session.get(AuthSession, foreign["id"]) is not None


async def test_revoke_keeps_noncurrent_history(clients):
    a, b, _ = clients
    await signup(a, "admin")
    await login(a, "admin")
    await login(b, "admin")
    row = next(row for row in (await a.get("/api/v1/me/sessions")).json() if not row["current"])
    response = await a.delete("/api/v1/me/sessions/" + row["id"])
    assert response.status_code == 200
    saved = next(item for item in (await a.get("/api/v1/me/sessions")).json() if item["id"] == row["id"])
    assert saved["revoked"] is True
    assert saved["active"] is False
    assert (await b.get("/api/v1/me")).status_code == 401


async def test_permanent_delete_revalidates_actor_after_waiting_for_lock(clients, monkeypatch):
    a, b, db = clients
    await signup(a, "admin")
    await login(a, "admin")
    await login(b, "admin")
    rows = (await a.get("/api/v1/me/sessions")).json()
    actor = next(row for row in rows if row["current"])
    target = next(row for row in rows if not row["current"])
    original_lock = db.locked
    entered = asyncio.Event()

    @asynccontextmanager
    async def observed_lock():
        if asyncio.current_task().get_name() == "stale-delete":
            entered.set()
        async with original_lock() as session:
            yield session

    monkeypatch.setattr(db, "locked", observed_lock)
    async with original_lock() as session:
        pending = asyncio.create_task(
            a.delete("/api/v1/me/sessions/" + target["id"] + "/permanent"),
            name="stale-delete",
        )
        await asyncio.wait_for(entered.wait(), 5)
        (await session.get(AuthSession, actor["id"])).revoked = True
    response = await asyncio.wait_for(pending, 5)
    assert response.status_code == 401
    async with db.session() as session:
        assert await session.get(AuthSession, target["id"]) is not None


async def test_session_metadata_upgrade_preserves_legacy_rows(tmp_path):
    db = Database(Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path}/legacy.db"))

    def initial_schema(connection):
        cfg = Config()
        cfg.set_main_option("script_location", str(PACKAGE_ROOT / "migrations"))
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "0001")

    try:
        async with db.engine.begin() as connection:
            await connection.run_sync(initial_schema)
            await connection.execute(text(
                "INSERT INTO users (id, username, email, password_hash, role, disabled, "
                "auth_version, scope_mode, mcp_ids, created_at) VALUES "
                "('legacy-user', 'legacy', '', 'unused', 'user', 0, 1, 'selected', '[]', CURRENT_TIMESTAMP)"
            ))
            await connection.execute(text(
                "INSERT INTO auth_sessions (id, user_id, auth_version, expires_at, revoked, created_at) "
                "VALUES ('legacy-session', 'legacy-user', 1, NULL, 0, CURRENT_TIMESTAMP)"
            ))
        await db.initialize()
        await db.initialize()
        async with db.session() as session:
            row = await session.get(AuthSession, "legacy-session")
            assert row is not None and row.user_id == "legacy-user"
            assert row.ip_address is None and row.user_agent is None
            assert not row.revoked and row.expires_at is None
            # An older application can still insert without the new fields.
            await session.execute(text(
                "INSERT INTO auth_sessions (id, user_id, auth_version, expires_at, revoked, created_at) "
                "VALUES ('old-writer', 'legacy-user', 1, NULL, 0, CURRENT_TIMESTAMP)"
            ))
            await session.commit()
            old_writer = await session.get(AuthSession, "old-writer")
            assert old_writer.ip_address is None and old_writer.user_agent is None
    finally:
        await db.close()


@pytest.mark.parametrize("url", ["sqlite:///example.db", "mysql://user:password@localhost/mcp"])
def test_session_metadata_migration_emits_additive_sql(tmp_path, monkeypatch, url):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)
    output = StringIO()
    cfg = Config(output_buffer=output)
    cfg.set_main_option("script_location", str(PACKAGE_ROOT / "migrations"))
    command.upgrade(cfg, "0001:head", sql=True)
    sql = output.getvalue()
    assert "ALTER TABLE auth_sessions ADD COLUMN ip_address VARCHAR(64)" in sql
    assert "ALTER TABLE auth_sessions ADD COLUMN user_agent VARCHAR(1024)" in sql
    assert "DROP TABLE" not in sql
