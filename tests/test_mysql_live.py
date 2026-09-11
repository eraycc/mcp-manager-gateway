import asyncio
import os

import httpx
import pytest
from sqlalchemy import select

from mcp_manager.app import create_app
from mcp_manager.cli import migrate_database
from mcp_manager.config import Settings
from mcp_manager.database import Database, User

URL = os.environ.get("MCP_TEST_MYSQL_URL")
pytestmark = pytest.mark.skipif(not URL, reason="MCP_TEST_MYSQL_URL is not set")


async def test_sqlite_mysql_round_trip_and_first_admin_race(tmp_path):
    source = Settings(data_dir=tmp_path / "source", secret_key="migration-test-key")
    app = create_app(source)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/api/v1/auth/register", json={"username": "admin", "password": "password12345"})
            assert response.status_code == 200
            await client.post("/api/v1/auth/login", json={"username": "admin", "password": "password12345"})
            client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]
            row = (await client.post("/api/v1/mcps", json={"name": "sealed", "transport": "stdio",
                    "config": {"command": "uv", "args": ["--version"], "env": {"SECRET": "preserved"}}})).json()
            token = (await client.post("/api/v1/tokens", json={"name": "migration", "scope_mode": "all"})).json()
    await migrate_database(source, URL)
    mysql = Settings(data_dir=tmp_path / "mysql", database_url=URL, secret_key=source.secret_key)
    app2 = create_app(mysql)
    async with app2.router.lifespan_context(app2):
        imported = await app2.state.catalog.get(row["id"])
        assert app2.state.catalog.unseal(imported.config)["env"]["SECRET"] == "preserved"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app2), base_url="http://test") as client:
            response = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "password12345"})
            assert response.status_code == 200, response.text
            client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]
            assert (await client.get("/api/v1/tokens")).json()["items"][0]["id"] == token["id"]
            responses = await asyncio.gather(*[client.post("/api/v1/auth/register",
                json={"username": "user" + str(i), "password": "password12345"}) for i in range(6)])
            assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
            assert all(r.json()["role"] == "user" for r in responses)
    target_url = "sqlite:///" + (tmp_path / "roundtrip.sqlite").as_posix()
    await migrate_database(mysql, target_url)
    target = Database(Settings(data_dir=tmp_path / "target", database_url=target_url, secret_key=source.secret_key))
    try:
        async with target.session() as session:
            users = list((await session.scalars(select(User))).all())
            assert len(users) == 7 and sum(u.role == "admin" for u in users) == 1
    finally:
        await target.close()
