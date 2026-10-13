"""Directory reads stay cached; explicit refresh discovers an authorized owner."""
import asyncio
from contextlib import asynccontextmanager

import httpx

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import McpServer


class Connection:
    async def discover(self):
        await asyncio.sleep(0.02)
        return {
            "tools": [{"name": "whoami", "inputSchema": {"type": "object"}}],
            "resources": [],
            "prompts": [],
            "templates": [],
        }

    async def ingest_catalog(self, store, scope, authorize=None):
        generation = await store.begin_refresh(scope)
        await store.stage_page(generation.id, "tools", [
            {"name": "whoami", "inputSchema": {"type": "object"}}
        ])
        await store.set_metadata(generation.id, {})
        await store.publish(generation.id, expected_scope=scope)
        return await store.get_summary(scope)

    async def call(self, name, arguments):
        return {"content": [{"type": "text", "text": "ok"}]}


@asynccontextmanager
async def discovery_console(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="first-use"))
    async with app.router.lifespan_context(app):
        calls = {"count": 0}

        @asynccontextmanager
        async def connect(spec):
            calls["count"] += 1
            yield Connection()

        app.state.runtime.connector = connect
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as web:
            actor = (await web.post(
                "/api/v1/auth/register",
                json={"username": "admin", "password": "password12345"},
            )).json()
            await web.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "password12345"},
            )
            web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
            created = await web.post("/api/v1/mcps", json={
                "name": "Personal",
                "transport": "streamable-http",
                "mode": "disabled",
                "isolation": "user",
                "config": {
                    "url": "https://mcp.test/mcp",
                    "auth": {"type": "bearer", "token": "alice-secret"},
                },
            })
            row = await app.state.catalog.get(created.json()["id"])
            async with app.state.db.locked() as session:
                saved = await session.get(McpServer, row.id)
                saved.mode = "lazy"
            row = await app.state.catalog.get(row.id)
            app.state.catalog.delete_cache(row, actor["id"])
            calls["count"] = 0
            yield app, web, actor, row, calls


async def test_tools_endpoint_is_read_only_and_explicit_refresh_discovers(tmp_path):
    async with discovery_console(tmp_path) as (_app, web, _actor, row, calls):
        responses = await asyncio.gather(*[
            web.get(f"/api/v1/mcps/{row.id}/tools") for _ in range(8)
        ])
        assert all(response.status_code == 200 for response in responses)
        assert calls["count"] == 0
        assert all(response.json()["cache_status"] == "empty" for response in responses)
        refreshed = await web.post(f"/api/v1/mcps/{row.id}/refresh")
        assert refreshed.status_code == 200 and refreshed.json()["status"] == "ready"
        assert refreshed.json()["tool_count"] == 1
        assert calls["count"] == 1
        cached = await web.get(f"/api/v1/mcps/{row.id}/capabilities?kind=tools")
        assert cached.status_code == 200 and cached.json()["items"][0]["name"] == "whoami"
        assert calls["count"] == 1


async def test_missing_personal_credentials_returns_status_not_empty_ready(tmp_path):
    async with discovery_console(tmp_path) as (app, web, actor, row, _calls):
        async with app.state.db.locked() as session:
            await app.state.credentials.delete_owner(
                session, actor["id"], server_id=row.id
            )
        response = await web.get(f"/api/v1/mcps/{row.id}/tools")
        assert response.status_code == 200
        assert response.json()["cache_status"] == "auth_required"
        assert response.json()["tools"] == []
