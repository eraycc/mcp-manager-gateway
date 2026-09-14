import asyncio
import hashlib
import sqlite3
from contextlib import asynccontextmanager

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request

from mcp_manager.config import PACKAGE_ROOT, Settings
from mcp_manager.database import ApiToken, AuthSession, Database
from mcp_manager.identity import authenticate_token, router
from mcp_manager.logs import LogStore


@pytest.fixture
async def clients(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path}/tokens.db")
    db = Database(settings)
    await db.initialize()
    app = FastAPI()
    app.state.db, app.state.config = db, settings
    app.state.logs = LogStore(tmp_path)
    app.include_router(router)

    @app.get("/probe")
    async def probe(request: Request):
        _user, token, _ids = await authenticate_token(request)
        return {"token_id": token.id if token else None}

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ordinary,
    ):
        yield app, admin, ordinary
    await app.state.logs.close()
    await db.close()


async def signup(client, username):
    response = await client.post("/api/v1/auth/register", json={
        "username": username, "password": "password123",
    })
    assert response.status_code == 200, response.text
    return response.json()


async def login(client, username):
    response = await client.post("/api/v1/auth/login", json={
        "username": username, "password": "password123",
    })
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]


async def test_lists_default_to_owner_and_admin_all_view_filters_explicit_owner(clients):
    _app, admin, ordinary = clients
    admin_user = await signup(admin, "AdminOwner")
    ordinary_user = await signup(ordinary, "OrdinaryOwner")
    await login(admin, "AdminOwner")
    await login(ordinary, "OrdinaryOwner")
    admin_token = (await admin.post("/api/v1/tokens", json={"name": "admin key"})).json()
    ordinary_token = (await ordinary.post("/api/v1/tokens", json={"name": "ordinary key"})).json()

    admin_default = (await admin.get("/api/v1/tokens")).json()
    ordinary_default = (await ordinary.get("/api/v1/tokens")).json()
    assert [(item["id"], item["username"]) for item in admin_default["items"]] == [
        (admin_token["id"], "AdminOwner")
    ]
    assert [(item["id"], item["username"]) for item in ordinary_default["items"]] == [
        (ordinary_token["id"], "OrdinaryOwner")
    ]
    assert admin_default["items"][0]["secret_available"] is True
    assert "token" not in admin_default["items"][0]
    assert "token_secret" not in admin_default["items"][0]

    assert (await ordinary.get("/api/v1/tokens", params={"view": "all"})).status_code == 403
    all_items = (await admin.get("/api/v1/tokens", params={"view": "all"})).json()["items"]
    assert {item["id"] for item in all_items} == {admin_token["id"], ordinary_token["id"]}
    by_username = (await admin.get("/api/v1/tokens", params={
        "view": "all", "username": "ordinary",
    })).json()["items"]
    assert [item["id"] for item in by_username] == [ordinary_token["id"]]
    by_user_id = (await admin.get("/api/v1/tokens", params={
        "view": "all", "user_id": ordinary_user["id"],
    })).json()["items"]
    assert [item["id"] for item in by_user_id] == [ordinary_token["id"]]
    literal_wildcard = (await admin.get("/api/v1/tokens", params={
        "view": "all", "username": "%",
    })).json()
    assert literal_wildcard["total"] == 0
    assert (await admin.get("/api/v1/tokens", params={
        "username": admin_user["username"],
    })).status_code == 422


async def test_create_reveal_rotate_store_only_ciphertext_and_keep_owner_boundaries(clients):
    app, admin, ordinary = clients
    await signup(admin, "admin")
    await signup(ordinary, "ordinary")
    await login(admin, "admin")
    await login(ordinary, "ordinary")
    creation = await ordinary.post("/api/v1/tokens", json={"name": "private"})
    assert creation.headers.get("cache-control") == "no-store"
    created = creation.json()
    original = created["token"]

    async with app.state.db.session() as session:
        row = await session.get(ApiToken, created["id"])
        first_ciphertext = row.token_secret
        assert first_ciphertext
        assert original not in first_ciphertext
        assert row.token_hash == hashlib.sha256(original.encode()).hexdigest()

    revealed = await ordinary.get(f"/api/v1/tokens/{created['id']}/secret")
    assert revealed.status_code == 200, revealed.text
    assert revealed.headers["cache-control"] == "no-store"
    assert revealed.json() == {"token": original}
    admin_reveal = await admin.get(f"/api/v1/tokens/{created['id']}/secret")
    assert admin_reveal.status_code == 200
    assert admin_reveal.json() == {"token": original}

    rotation = await ordinary.post(f"/api/v1/tokens/{created['id']}/rotate")
    assert rotation.headers.get("cache-control") == "no-store"
    rotated = rotation.json()
    assert rotated["token"] != original
    async with app.state.db.session() as session:
        row = await session.get(ApiToken, created["id"])
        assert row.token_secret != first_ciphertext
        assert rotated["token"] not in row.token_secret
    assert (await ordinary.get(f"/api/v1/tokens/{created['id']}/secret")).json() == {
        "token": rotated["token"]
    }
    assert (await admin.get("/probe", headers={"Authorization": "Bearer " + original})).status_code == 401
    assert (await admin.get("/probe", headers={
        "Authorization": "Bearer " + rotated["token"],
    })).status_code == 200

    admin_token = (await admin.post("/api/v1/tokens", json={"name": "admin-only"})).json()
    for method, path, payload in [
        ("GET", f"/api/v1/tokens/{admin_token['id']}/secret", None),
        ("PATCH", f"/api/v1/tokens/{admin_token['id']}", {"name": "stolen"}),
        ("DELETE", f"/api/v1/tokens/{admin_token['id']}", None),
        ("POST", f"/api/v1/tokens/{admin_token['id']}/rotate", None),
    ]:
        assert (await ordinary.request(method, path, json=payload)).status_code == 404


async def test_legacy_hash_only_token_authenticates_but_cannot_be_revealed(clients):
    app, admin, _ordinary = clients
    await signup(admin, "admin")
    await login(admin, "admin")
    created = (await admin.post("/api/v1/tokens", json={"name": "legacy"})).json()
    async with app.state.db.locked() as session:
        row = await session.get(ApiToken, created["id"])
        row.token_secret = None

    response = await admin.get(f"/api/v1/tokens/{created['id']}/secret")
    assert response.status_code == 409
    assert response.json() == {
        "detail": "This legacy token secret cannot be recovered; rotate it once.",
        "code": "token_secret_unavailable",
    }
    assert (await admin.get("/probe", headers={
        "Authorization": "Bearer " + created["token"],
    })).status_code == 200
    async with app.state.db.session() as session:
        assert (await session.get(ApiToken, created["id"])).token_secret is None


async def test_reveal_revalidates_revoked_session_after_lock_wait(clients, monkeypatch):
    app, admin, _ordinary = clients
    await signup(admin, "admin")
    await login(admin, "admin")
    created = (await admin.post("/api/v1/tokens", json={"name": "secret"})).json()
    session_id = (await admin.get("/api/v1/me/sessions")).json()[0]["id"]
    original_lock = app.state.db.locked
    entered = asyncio.Event()

    @asynccontextmanager
    async def observed_lock():
        if asyncio.current_task().get_name() == "stale-reveal":
            entered.set()
        async with original_lock() as session:
            yield session

    monkeypatch.setattr(app.state.db, "locked", observed_lock)
    async with original_lock() as session:
        task = asyncio.create_task(
            admin.get(f"/api/v1/tokens/{created['id']}/secret"),
            name="stale-reveal",
        )
        await asyncio.wait_for(entered.wait(), 3)
        (await session.get(AuthSession, session_id)).revoked = True
    response = await asyncio.wait_for(task, 3)
    assert response.status_code == 401
    assert created["token"] not in response.text


async def test_migration_adds_nullable_secret_without_inventing_legacy_value(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy.db"
    database_url = "sqlite:///" + database_path.as_posix()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", database_url)
    cfg = Config()
    cfg.set_main_option("script_location", str(PACKAGE_ROOT / "migrations"))
    await asyncio.to_thread(command.upgrade, cfg, "0002")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("user-id", "legacy", "", "unused", "admin", 0, 1, "all", "[]", "2026-01-01"),
        )
        connection.execute(
            "INSERT INTO api_tokens VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "token-id", "user-id", "legacy", "mcpm_legacy",
                hashlib.sha256(b"mcpm_legacy_secret").hexdigest(), 0, None,
                "all", "[]", "native", "2026-01-01",
            ),
        )
        connection.commit()

    await asyncio.to_thread(command.upgrade, cfg, "head")
    settings = Settings(data_dir=tmp_path, database_url=database_url)
    db = Database(settings)
    async with db.session() as session:
        token = await session.get(ApiToken, "token-id")
        assert token.token_secret is None
        assert token.token_hash == hashlib.sha256(b"mcpm_legacy_secret").hexdigest()
    await db.close()
