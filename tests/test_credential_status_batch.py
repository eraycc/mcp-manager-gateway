"""Credential status batches are bounded and never disclose unassigned services."""
from sqlalchemy import select

from mcp_manager.database import User


async def create_service(client, name):
    response = await client.post("/api/v1/mcps", json={
        "name": name,
        "transport": "streamable-http",
        "isolation": "user",
        "mode": "disabled",
        "config": {
            "url": "https://mcp.test/mcp",
            "auth": {"type": "bearer", "token": name + "-secret"},
        },
    })
    assert response.status_code == 200, response.text
    return response.json()


async def test_status_batch_is_bounded_and_omits_unauthorized_ids(db_app):
    allowed = await create_service(db_app.web, "Allowed")
    hidden = await create_service(db_app.web, "Hidden")
    registered = await db_app.web.post(
        "/api/v1/auth/register",
        json={"username": "reader", "password": "password12345"},
    )
    assert registered.status_code == 200
    async with db_app.db.locked() as session:
        reader = await session.scalar(select(User).where(User.username == "reader"))
        reader.scope_mode = "selected"
        reader.mcp_ids = [allowed["id"]]
    await db_app.web.post(
        "/api/v1/auth/login",
        json={"username": "reader", "password": "password12345"},
    )
    db_app.web.headers["X-CSRF-Token"] = db_app.web.cookies["mcp_csrf"]
    response = await db_app.web.post(
        "/api/v1/mcps/credentials/status-batch",
        json={"ids": [allowed["id"], hidden["id"]]},
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [allowed["id"]]
    assert hidden["id"] not in response.text

    oversized = await db_app.web.post(
        "/api/v1/mcps/credentials/status-batch",
        json={"ids": [str(index) for index in range(51)]},
    )
    assert oversized.status_code == 422
