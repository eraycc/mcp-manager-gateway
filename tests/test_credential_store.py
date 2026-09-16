"""Sealed service and personal credentials are separated from public MCP config."""
import time

import pytest

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import McpServer, get_setting, set_setting
from mcp_manager.runtime import GatewayError


async def disabled_row(app, *, isolation="user"):
    return await app.state.catalog.create({
        "name": "credential test",
        "transport": "streamable-http",
        "mode": "disabled",
        "isolation": isolation,
        "config": {"url": "https://mcp.test/mcp", "auth": {"type": "none"}},
    })


def credential_store(app):
    store = getattr(app.state, "credentials", None)
    assert store is not None, "application credential store is missing"
    return store


@pytest.mark.asyncio
async def test_user_bearer_is_removed_from_server_config(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="credential-store"))
    async with app.router.lifespan_context(app):
        row = await disabled_row(app)
        store = credential_store(app)
        async with app.state.db.locked() as session:
            saved = await session.get(McpServer, row.id)
            stored = await store.persist_config(
                session,
                saved,
                {"url": "https://mcp.test/mcp",
                 "auth": {"type": "bearer", "token": "alice-secret"}},
                "user",
                "alice",
            )
            saved.config = app.state.catalog.seal(stored)
        assert stored["auth"] == {"type": "bearer"}
        assert "alice-secret" not in str(stored)
        payload = await store.load(row.id, "alice")
        assert payload["auth_config"]["token"] == "alice-secret"


@pytest.mark.asyncio
async def test_shared_oauth_splits_service_config_and_user_token(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="credential-store-shared"))
    async with app.router.lifespan_context(app):
        row = await disabled_row(app)
        store = credential_store(app)
        full = {"url": "https://mcp.test/mcp", "auth": {
            "type": "oauth",
            "config_isolation": "shared",
            "authorization_url": "https://auth.test/authorize",
            "token_url": "https://auth.test/token",
            "client_id": "shared-client",
            "client_secret": "service-secret",
            "access_token": "alice-access",
            "refresh_token": "alice-refresh",
            "expires_at": time.time() + 3600,
        }}
        async with app.state.db.locked() as session:
            saved = await session.get(McpServer, row.id)
            stored = await store.persist_config(session, saved, full, "user", "alice")
            saved.config = app.state.catalog.seal(stored)
            saved.isolation = "user"
        service = await store.load(row.id, "service")
        personal = await store.load(row.id, "alice")
        assert stored["auth"] == {"type": "oauth", "config_isolation": "shared"}
        assert service["auth_config"]["client_secret"] == "service-secret"
        assert "access_token" not in service["auth_config"]
        assert personal["oauth_token"]["access_token"] == "alice-access"
        assert "auth_config" not in personal


@pytest.mark.asyncio
async def test_private_oauth_keeps_config_and_token_under_user(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="credential-store-private"))
    async with app.router.lifespan_context(app):
        row = await disabled_row(app)
        store = credential_store(app)
        full = {"url": "https://mcp.test/mcp", "auth": {
            "type": "oauth",
            "config_isolation": "user",
            "authorization_url": "https://auth.test/authorize",
            "token_url": "https://auth.test/token",
            "client_id": "personal-client",
            "client_secret": "personal-secret",
            "access_token": "personal-access",
            "expires_at": time.time() + 3600,
        }}
        async with app.state.db.locked() as session:
            saved = await session.get(McpServer, row.id)
            stored = await store.persist_config(session, saved, full, "user", "alice")
            saved.config = app.state.catalog.seal(stored)
            saved.isolation = "user"
        assert await store.load(row.id, "service") == {}
        personal = await store.load(row.id, "alice")
        assert personal["auth_config"]["client_secret"] == "personal-secret"
        assert personal["oauth_token"]["access_token"] == "personal-access"


@pytest.mark.asyncio
async def test_legacy_oauth_token_migrates_after_successful_new_write(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="credential-store-legacy"))
    async with app.router.lifespan_context(app):
        row = await app.state.catalog.create({
            "name": "legacy OAuth",
            "transport": "streamable-http",
            "mode": "disabled",
            "isolation": "user",
            "config": {"url": "https://mcp.test/mcp", "auth": {
                "type": "oauth",
                "authorization_url": "https://auth.test/authorize",
                "token_url": "https://auth.test/token",
                "client_id": "legacy-client",
            }},
        }, user_id="alice")
        legacy_key = "oauth:" + row.id + ":alice"
        await set_setting(app.state.db, legacy_key, app.state.catalog.seal(
            {"access_token": "legacy-access", "expires_at": time.time() + 3600}))
        resolved = await credential_store(app).materialize(row, "alice")
        assert resolved["auth"]["client_id"] == "legacy-client"
        assert resolved["auth"]["access_token"] == "legacy-access"
        assert await get_setting(app.state.db, credential_store(app).key(row.id, "alice"))
        assert await get_setting(app.state.db, legacy_key) is None


@pytest.mark.asyncio
async def test_service_bearer_transition_copies_actor_then_removes_global(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="credential-transition"))
    async with app.router.lifespan_context(app):
        row = await disabled_row(app, isolation="service")
        store = credential_store(app)
        async with app.state.db.locked() as session:
            saved = await session.get(McpServer, row.id)
            global_stored = await store.persist_config(
                session,
                saved,
                {"url": "https://mcp.test/mcp",
                 "auth": {"type": "bearer", "token": "service-secret"}},
                "service",
                "admin",
            )
            saved.config = app.state.catalog.seal(global_stored)
        row = await app.state.catalog.get(row.id)
        resolved = await store.materialize(row, "admin")
        async with app.state.db.locked() as session:
            saved = await session.get(McpServer, row.id)
            personal_stored = await store.persist_config(
                session, saved, resolved, "user", "admin")
            saved.config = app.state.catalog.seal(personal_stored)
            saved.isolation = "user"
        assert await store.load(row.id, "service") == {}
        assert (await store.load(row.id, "admin"))["auth_config"]["token"] == "service-secret"


@pytest.mark.asyncio
async def test_oauth_token_helpers_list_and_delete_exact_owner(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="credential-helpers"))
    async with app.router.lifespan_context(app):
        row = await disabled_row(app)
        store = credential_store(app)
        async with app.state.db.locked() as session:
            saved = await session.get(McpServer, row.id)
            stored = await store.persist_config(session, saved, {
                "url": "https://mcp.test/mcp",
                "auth": {
                    "type": "oauth",
                    "config_isolation": "shared",
                    "authorization_url": "https://auth.test/authorize",
                    "token_url": "https://auth.test/token",
                    "client_id": "shared-client",
                },
            }, "user", "alice")
            saved.config = app.state.catalog.seal(stored)
            owner = await store.save_oauth_token(
                session, saved, "alice", {"access_token": "alice-access"})
        assert owner == "alice"
        assert await store.owners(row.id) == {"alice", "service"}
        async with app.state.db.locked() as session:
            await store.delete_owner(session, "alice", server_id=row.id)
        assert await store.load(row.id, "alice") == {}
        assert (await store.load(row.id, "service"))["auth_config"]["client_id"] == "shared-client"


@pytest.mark.asyncio
async def test_missing_personal_bearer_returns_auth_required(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="credential-store-missing"))
    async with app.router.lifespan_context(app):
        row = await disabled_row(app)
        async with app.state.db.locked() as session:
            saved = await session.get(McpServer, row.id)
            saved.config = app.state.catalog.seal({
                "url": "https://mcp.test/mcp", "auth": {"type": "bearer"}})
        row = await app.state.catalog.get(row.id)
        with pytest.raises(GatewayError) as caught:
            await credential_store(app).materialize(row, "bob")
        assert caught.value.code == "auth_required"
