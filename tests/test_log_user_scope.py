import json

import httpx
import pytest
from fastapi import FastAPI

from mcp_manager.config import Settings
from mcp_manager.database import Database
from mcp_manager.identity import router as identity_router
from mcp_manager.logs import LogStore
from mcp_manager.operations_api import router as operations_router


@pytest.fixture
async def log_clients(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path}/logs-api.db")
    db = Database(settings)
    await db.initialize()
    app = FastAPI()
    app.state.db, app.state.config = db, settings
    app.state.logs = LogStore(tmp_path)
    app.include_router(identity_router)
    app.include_router(operations_router)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ordinary,
    ):
        admin_user = (await admin.post("/api/v1/auth/register", json={
            "username": "AdminOwner", "password": "password123",
        })).json()
        ordinary_user = (await ordinary.post("/api/v1/auth/register", json={
            "username": "OrdinaryOwner", "password": "password123",
        })).json()
        for client, username in ((admin, "AdminOwner"), (ordinary, "OrdinaryOwner")):
            assert (await client.post("/api/v1/auth/login", json={
                "username": username, "password": "password123",
            })).status_code == 200
            client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]
        await app.state.logs.append({
            "id": "admin-log", "user_id": admin_user["id"], "username": "AdminOwner",
            "tool_name": "admin_tool", "status": "success",
        })
        await app.state.logs.append({
            "id": "ordinary-log", "user_id": ordinary_user["id"], "username": "OrdinaryOwner",
            "tool_name": "ordinary_tool", "status": "success",
        })
        yield app, admin, ordinary, admin_user, ordinary_user
    await app.state.logs.close()
    await db.close()


async def test_admin_username_filter_is_literal_case_insensitive_and_ordinary_scope_cannot_expand(log_clients):
    _app, admin, ordinary, admin_user, ordinary_user = log_clients

    all_logs = (await admin.get("/api/v1/logs")).json()
    assert {item["id"] for item in all_logs["items"]} == {"admin-log", "ordinary-log"}
    filtered = (await admin.get("/api/v1/logs", params={"username": "ordinary"})).json()
    assert [item["id"] for item in filtered["items"]] == ["ordinary-log"]
    assert (await admin.get("/api/v1/logs", params={"username": "%"})).json()["total"] == 0

    own = (await ordinary.get("/api/v1/logs", params={
        "user_id": admin_user["id"], "q": "tool",
    })).json()
    assert [item["id"] for item in own["items"]] == ["ordinary-log"]
    narrowed = (await ordinary.get("/api/v1/logs", params={
        "user_id": admin_user["id"], "username": "AdminOwner",
    })).json()
    assert narrowed["total"] == 0
    assert ordinary_user["id"] != admin_user["id"]


async def test_list_export_and_detail_keep_user_scope_with_username_filter(log_clients):
    _app, admin, ordinary, _admin_user, ordinary_user = log_clients

    admin_export = await admin.get("/api/v1/logs/export", params={"username": "ADMIN"})
    assert admin_export.status_code == 200
    assert [item["id"] for item in json.loads(admin_export.text)] == ["admin-log"]

    ordinary_export = await ordinary.get("/api/v1/logs/export", params={
        "user_id": "other", "username": "Ordinary",
    })
    assert ordinary_export.status_code == 200
    assert [item["id"] for item in json.loads(ordinary_export.text)] == ["ordinary-log"]
    assert (await ordinary.get("/api/v1/logs/admin-log")).status_code == 404
    detail = await ordinary.get("/api/v1/logs/ordinary-log")
    assert detail.status_code == 200
    assert detail.json()["user_id"] == ordinary_user["id"]
    assert (await admin.get("/api/v1/logs/ordinary-log")).status_code == 200
