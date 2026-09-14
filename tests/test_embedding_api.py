import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select

from mcp_manager.catalog import Catalog
from mcp_manager.config import Settings
from mcp_manager.database import Database, SystemSetting, User
from mcp_manager.embedding_api import load_embedding_config, router
from mcp_manager.embeddings import EmbeddingIndex
from mcp_manager.identity import admin_user
from mcp_manager.identity import router as identity_router


class Unused:
    pass


@pytest.fixture
async def api(tmp_path):
    settings = Settings(home_dir=tmp_path, data_dir=tmp_path, public_url="http://test")
    app = FastAPI()
    app.state.config = settings
    app.state.db = Database(settings)
    await app.state.db.initialize()
    app.state.catalog = Catalog(app.state.db, Unused(), Unused(), settings)
    app.state.embeddings = EmbeddingIndex(tmp_path)
    app.include_router(identity_router)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield app, client
    await app.state.db.close()


async def login_admin(client):
    response = await client.post("/api/v1/auth/register", json={
        "username": "admin", "password": "password123"
    })
    assert response.status_code == 200
    response = await client.post("/api/v1/auth/login", json={
        "username": "admin", "password": "password123"
    })
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]


async def test_embedding_settings_require_admin(api):
    _app, client = api
    assert (await client.get("/api/v1/settings/embedding")).status_code == 401
    assert (await client.patch("/api/v1/settings/embedding", json={})).status_code == 401
    assert (await client.post("/api/v1/settings/embedding/test")).status_code == 401


async def test_secret_is_masked_sealed_and_redacted_patch_preserves_it(api):
    app, client = api
    await login_admin(client)
    saved = {
        "enabled": True,
        "base_url": "http://localhost:1234/v1",
        "model": "local-model",
        "api_key": "top-secret-key",
        "timeout_seconds": 7,
        "min_similarity": 0.25,
    }

    response = await client.patch("/api/v1/settings/embedding", json=saved)
    assert response.status_code == 200, response.text
    assert response.json() == saved | {"api_key": "[REDACTED]"}
    async with app.state.db.session() as session:
        row = await session.get(SystemSetting, "discovery_embedding")
        assert set(row.value) == {"sealed"}
        assert "top-secret-key" not in json.dumps(row.value)
        assert app.state.catalog.unseal(row.value) == saved

    response = await client.patch("/api/v1/settings/embedding", json={
        "model": "local-model-v2", "api_key": "[REDACTED]"
    })
    assert response.status_code == 200, response.text
    assert response.json()["api_key"] == "[REDACTED]"
    loaded = await load_embedding_config(app.state)
    assert loaded["api_key"] == "top-secret-key"
    assert loaded["model"] == "local-model-v2"

    response = await client.patch("/api/v1/settings/embedding", json={"api_key": ""})
    assert response.status_code == 200
    assert response.json()["api_key"] == ""
    assert (await load_embedding_config(app.state))["api_key"] == ""


async def test_masked_patch_merges_inside_write_lock_and_keeps_concurrent_key_rotation(api):
    app, client = api
    await login_admin(client)
    await client.patch("/api/v1/settings/embedding", json={
        "api_key": "old-key",
    })
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_admin(request: Request):
        actor = await admin_user(request)
        entered.set()
        await release.wait()
        return actor

    app.dependency_overrides[admin_user] = delayed_admin
    task = asyncio.create_task(client.patch("/api/v1/settings/embedding", json={
        "model": "new-model",
        "api_key": "[REDACTED]",
    }))
    await asyncio.wait_for(entered.wait(), 3)
    async with app.state.db.locked() as session:
        row = await session.get(SystemSetting, "discovery_embedding")
        rotated = app.state.catalog.unseal(row.value)
        rotated["api_key"] = "rotated-key"
        row.value = app.state.catalog.seal(rotated)
        release.set()
        await asyncio.sleep(0)

    response = await asyncio.wait_for(task, 3)
    assert response.status_code == 200, response.text
    assert (await load_embedding_config(app.state))["api_key"] == "rotated-key"


@pytest.mark.parametrize("payload", [
    {"unknown": True},
    {"enabled": "yes"},
    {"timeout_seconds": True},
    {"timeout_seconds": 0},
    {"timeout_seconds": 61},
    {"min_similarity": -1.1},
    {"min_similarity": 1.1},
    {"base_url": "ftp://embedding.test/v1"},
    {"base_url": "http://"},
])
async def test_patch_rejects_unknown_or_invalid_values(api, payload):
    _app, client = api
    await login_admin(client)
    response = await client.patch("/api/v1/settings/embedding", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("payload", [
    {"enabled": True, "base_url": "", "model": "model"},
    {"enabled": True, "base_url": "http://embedding.test/v1", "model": ""},
])
async def test_enabled_settings_require_endpoint_and_model(api, payload):
    _app, client = api
    await login_admin(client)
    response = await client.patch("/api/v1/settings/embedding", json=payload)
    assert response.status_code == 422


async def test_probe_revalidates_admin_immediately_before_network_dispatch(api):
    app, client = api
    await login_admin(client)
    await client.patch("/api/v1/settings/embedding", json={
        "enabled": True,
        "base_url": "http://embedding.test/v1",
        "model": "saved-model",
    })
    network_calls = []
    app.state.embeddings._transport = httpx.MockTransport(
        lambda request: network_calls.append(request) or httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0]}]}
        )
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def stale_admin(request: Request):
        actor = await admin_user(request)
        entered.set()
        await release.wait()
        return actor

    app.dependency_overrides[admin_user] = stale_admin
    task = asyncio.create_task(client.post("/api/v1/settings/embedding/test"))
    await asyncio.wait_for(entered.wait(), 3)
    async with app.state.db.locked() as session:
        actor = await session.get(User, (await admin_user_from_database(app)).id)
        actor.disabled = True
    release.set()

    response = await asyncio.wait_for(task, 3)
    assert response.status_code == 401
    assert network_calls == []


async def admin_user_from_database(app):
    async with app.state.db.session() as session:
        return (await session.execute(select(User).where(User.username == "admin"))).scalar_one()


async def test_defaults_have_no_paid_endpoint_and_test_uses_saved_settings(api):
    app, client = api
    await login_admin(client)
    response = await client.get("/api/v1/settings/embedding")
    assert response.json() == {
        "enabled": False,
        "base_url": "",
        "model": "",
        "api_key": "",
        "timeout_seconds": 10.0,
        "min_similarity": 0.5,
    }

    await client.patch("/api/v1/settings/embedding", json={
        "enabled": True,
        "base_url": "http://embedding.test/v1",
        "model": "saved-model",
        "api_key": "saved-secret",
    })

    def handler(request):
        payload = json.loads(request.content)
        assert payload["model"] == "saved-model"
        assert request.headers["authorization"] == "Bearer saved-secret"
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    app.state.embeddings._transport = httpx.MockTransport(handler)
    response = await client.post("/api/v1/settings/embedding/test")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ready",
        "semantic_status": "ready",
        "dimension": 2,
        "model": "saved-model",
        "endpoint": "http://embedding.test/v1/embeddings",
    }
    assert "saved-secret" not in response.text
