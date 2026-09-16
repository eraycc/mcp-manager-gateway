"""Profile credential endpoints enforce user ownership and revision safety."""
from contextlib import asynccontextmanager

import httpx
import pytest

from mcp_manager.app import create_app
from mcp_manager.config import Settings


class Connection:
    async def discover(self):
        return {
            "tools": [{"name": "whoami", "inputSchema": {"type": "object"}}],
            "resources": [],
            "prompts": [],
            "templates": [],
        }

    async def call(self, name, arguments):
        return {"content": [{"type": "text", "text": "ok"}]}


@asynccontextmanager
async def credential_console(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="personal-api"))
    async with app.router.lifespan_context(app):
        @asynccontextmanager
        async def connect(spec):
            yield Connection()

        app.state.runtime.connector = connect
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            actor = (await client.post(
                "/api/v1/auth/register",
                json={"username": "admin", "password": "password12345"},
            )).json()
            await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "password12345"},
            )
            client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]
            yield app, client, actor


async def create_bearer(client, isolation="user"):
    response = await client.post("/api/v1/mcps", json={
        "name": "Bearer " + isolation,
        "transport": "streamable-http",
        "isolation": isolation,
        "mode": "disabled",
        "config": {
            "url": "https://mcp.test/mcp",
            "auth": {"type": "bearer", "token": "initial-secret"},
        },
    })
    assert response.status_code == 200, response.text
    return response.json()


async def test_user_can_replace_only_own_user_isolated_bearer(tmp_path):
    async with credential_console(tmp_path) as (app, client, actor):
        row = await create_bearer(client)
        response = await client.put(
            f"/api/v1/mcps/{row['id']}/credentials",
            json={
                "revision": row["revision"],
                "auth": {"type": "bearer", "token": "replacement-secret"},
            },
        )
        assert response.status_code == 200, response.text
        assert "replacement-secret" not in response.text
        personal = await app.state.credentials.load(row["id"], actor["id"])
        assert personal["auth_config"]["token"] == "replacement-secret"
        status = await client.get(
            f"/api/v1/mcps/{row['id']}/credentials/status"
        )
        assert status.status_code == 200
        assert status.json()["required"] is True


async def test_personal_endpoint_rejects_global_service_and_stale_revision(tmp_path):
    async with credential_console(tmp_path) as (_app, client, _actor):
        personal = await create_bearer(client)
        global_row = await create_bearer(client, isolation="service")
        global_response = await client.put(
            f"/api/v1/mcps/{global_row['id']}/credentials",
            json={
                "revision": global_row["revision"],
                "auth": {"type": "bearer", "token": "not-allowed"},
            },
        )
        stale_response = await client.put(
            f"/api/v1/mcps/{personal['id']}/credentials",
            json={
                "revision": personal["revision"] - 1,
                "auth": {"type": "bearer", "token": "stale"},
            },
        )
        assert global_response.status_code == 422
        assert stale_response.status_code == 409


@pytest.mark.parametrize(
    ("auth", "isolation", "required"),
    [
        ({"type": "none"}, "service", False),
        ({"type": "none"}, "user", False),
        ({"type": "bearer", "token": "x"}, "service", False),
        ({"type": "bearer", "token": "x"}, "user", True),
    ],
)
async def test_profile_credential_status_matrix(tmp_path, auth, isolation, required):
    async with credential_console(tmp_path) as (_app, client, _actor):
        response = await client.post("/api/v1/mcps", json={
            "name": f"{auth['type']} {isolation}",
            "transport": "streamable-http",
            "isolation": isolation,
            "mode": "disabled",
            "config": {"url": "https://mcp.test/mcp", "auth": auth},
        })
        assert response.status_code == 200, response.text
        row = response.json()
        status = await client.get(
            f"/api/v1/mcps/{row['id']}/credentials/status"
        )
        assert status.status_code == 200
        assert status.json()["required"] is required
