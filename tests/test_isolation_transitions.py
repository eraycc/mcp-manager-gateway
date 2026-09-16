"""Isolation transitions move credentials atomically and invalidate old catalogs."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.runtime import GatewayError


class Connection:
    def __init__(self, spec):
        self.spec = spec

    async def discover(self):
        token = self.spec.config.get("auth", {}).get("token", "none")
        return {
            "tools": [{"name": token.removesuffix("-token") + "-tool",
                       "inputSchema": {"type": "object"}}],
            "resources": [],
            "templates": [],
            "prompts": [],
        }

    async def call(self, name, arguments):
        return {"content": [{"type": "text", "text": "ok"}]}


@asynccontextmanager
async def isolation_console(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="isolation-transition"))
    async with app.router.lifespan_context(app):
        @asynccontextmanager
        async def connect(spec):
            yield Connection(spec)

        app.state.runtime.connector = connect
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as web:
            actor = (await web.post(
                "/api/v1/auth/register",
                json={"username": "admin", "password": "password12345"},
            )).json()
            yield app, actor["id"]


def bearer_config(token):
    return {
        "url": "https://mcp.test/mcp",
        "auth": {"type": "bearer", "token": token},
    }


async def test_user_to_service_promotes_actor_and_clears_scoped_catalogs(tmp_path):
    async with isolation_console(tmp_path) as (app, admin_id):
        row = await app.state.catalog.create({
            "name": "personal",
            "transport": "streamable-http",
            "isolation": "user",
            "config": bearer_config("admin-token"),
        }, user_id=admin_id)
        async with app.state.db.locked() as session:
            await app.state.credentials.write(session, row.id, "bob", {
                "auth_config": {"type": "bearer", "token": "bob-token"}
            })
        app.state.catalog.save_cache(row, {
            "tools": [{"name": "bob-tool", "inputSchema": {"type": "object"}}],
            "cache_status": "ready",
        }, "bob")

        changed = await app.state.catalog.update(row.id, {
            "revision": row.revision,
            "isolation": "service",
            "config": bearer_config("[REDACTED]"),
        }, user_id=admin_id)

        spec = await app.state.catalog.spec(changed, admin_id)
        assert spec.isolation == "service"
        assert spec.credential_owner == "service"
        assert spec.config["auth"]["token"] == "admin-token"
        keys = [key for key in app.state.catalog.cache_store.items if key.startswith(row.id + "-")]
        assert keys == [app.state.catalog.cache_key(changed)]
        assert [tool["name"] for tool in app.state.catalog.cached(changed, "bob")["tools"]] == [
            "admin-tool"
        ]


async def test_service_to_user_copies_only_actor_and_drops_global_catalog(tmp_path):
    async with isolation_console(tmp_path) as (app, admin_id):
        row = await app.state.catalog.create({
            "name": "global",
            "transport": "streamable-http",
            "isolation": "service",
            "config": bearer_config("service-token"),
        }, user_id=admin_id)

        changed = await app.state.catalog.update(row.id, {
            "revision": row.revision,
            "isolation": "user",
            "config": bearer_config("[REDACTED]"),
        }, user_id=admin_id)

        admin = await app.state.catalog.spec(changed, admin_id)
        assert admin.isolation == "user"
        assert admin.credential_owner == admin_id
        assert admin.config["auth"]["token"] == "service-token"
        assert app.state.catalog.cached(changed, "bob")["cache_status"] != "ready"
        with pytest.raises(GatewayError) as caught:
            await app.state.catalog.spec(changed, "bob")
        assert caught.value.code == "auth_required"


async def test_failed_credential_migration_rolls_back_server_revision(tmp_path, monkeypatch):
    async with isolation_console(tmp_path) as (app, admin_id):
        row = await app.state.catalog.create({
            "name": "rollback",
            "transport": "streamable-http",
            "isolation": "service",
            "mode": "disabled",
            "config": bearer_config("service-token"),
        }, user_id=admin_id)
        before_keys = set(app.state.catalog.cache_store.items)
        monkeypatch.setattr(
            app.state.credentials,
            "write",
            AsyncMock(side_effect=OSError("credential store unavailable")),
        )

        with pytest.raises(OSError, match="credential store unavailable"):
            await app.state.catalog.update(row.id, {
                "revision": row.revision,
                "isolation": "user",
                "config": bearer_config("[REDACTED]"),
            }, user_id=admin_id)

        after = await app.state.catalog.get(row.id)
        assert (after.isolation, after.revision) == (row.isolation, row.revision)
        assert set(app.state.catalog.cache_store.items) == before_keys
