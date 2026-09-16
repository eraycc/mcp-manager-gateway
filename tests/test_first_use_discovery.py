"""The first authorized directory read discovers one owner exactly once."""
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


async def test_tools_endpoint_discovers_ready_personal_credentials_once(tmp_path):
    async with discovery_console(tmp_path) as (_app, web, _actor, row, calls):
        responses = await asyncio.gather(*[
            web.get(f"/api/v1/mcps/{row.id}/tools") for _ in range(8)
        ])
        assert all(response.status_code == 200 for response in responses)
        assert calls["count"] == 1
        assert all(
            response.json()["cache_status"] == "ready"
            for response in responses
        )


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
