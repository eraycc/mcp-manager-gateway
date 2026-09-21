import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import ApiToken, McpServer, User
from mcp_manager.logs import redact
from mcp_manager.proposals import list_proposals, submit_proposals


@pytest.fixture
async def requirement5_web(tmp_path):
    app = create_app(Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path}/requirement5.db",
        secret_key="requirement5-secret",
        public_url="http://test",
    ))
    ready, finished = asyncio.Event(), asyncio.Event()

    async def lifespan_owner():
        async with app.router.lifespan_context(app):
            ready.set()
            await finished.wait()

    owner = asyncio.create_task(lifespan_owner())
    await asyncio.wait_for(ready.wait(), 10)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/v1/auth/register", json={
                "username": "admin", "password": "password12345",
            })
            await client.post("/api/v1/auth/login", json={
                "username": "admin", "password": "password12345",
            })
            client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]
            created = await client.post("/api/v1/tokens", json={
                "name": "requirement5", "scope_mode": "all",
                "enable_mcp_proposal": True,
            })
            async with app.state.db.session() as session:
                user = await session.scalar(select(User).where(User.username == "admin"))
                token = await session.get(ApiToken, created.json()["id"])

            async def submit(value):
                result = await submit_proposals(app, user, token, value)
                return result["items"][0]["id"]

            yield app, client, user, submit
    finally:
        finished.set()
        await asyncio.wait_for(owner, 10)


@pytest.mark.asyncio
async def test_generated_slug_is_stable_and_uses_numeric_conflicts(requirement5_web):
    app, client, _user, submit = requirement5_web
    first = await app.state.catalog.create({
        "name": "My Fancy MCP!", "transport": "stdio", "mode": "disabled",
        "config": {"command": "first"},
    })
    second = await app.state.catalog.create({
        "name": "My Fancy MCP!", "transport": "stdio", "mode": "disabled",
        "config": {"command": "second"},
    })
    assert first.slug == "my-fancy-mcp"
    assert second.slug == "my-fancy-mcp-2"

    proposal_id = await submit({
        "name": "Manual slug", "slug": None, "transport": "stdio",
        "config": {"command": "manual"},
    })
    response = await client.patch(f"/api/v1/mcp-proposals/{proposal_id}", json={
        "slug": first.slug, "mode": "lazy", "isolation": "service",
    })
    assert response.status_code == 409
    assert "slug" in response.text.lower()


@pytest.mark.asyncio
async def test_secret_reveal_preserve_approve_and_log_redaction(requirement5_web):
    app, client, user, submit = requirement5_web
    proposal_id = await submit({
        "name": "Secret Service", "transport": "streamable-http",
        "description": "original",
        "config": {
            "url": "https://example.test/mcp",
            "headers": {"X-Private": "header-secret"},
            "env": {"PRIVATE_ENV": "env-secret"},
            "auth": {"type": "bearer", "token": "bearer-secret"},
        },
    })

    hidden = (await client.get(f"/api/v1/mcp-proposals/{proposal_id}")).json()
    assert hidden["payload"]["config"]["headers"]["X-Private"] == "[REDACTED]"
    assert hidden["payload"]["config"]["env"]["PRIVATE_ENV"] == "[REDACTED]"
    assert hidden["payload"]["config"]["auth"]["token"] == "[REDACTED]"

    shown_response = await client.get(
        f"/api/v1/mcp-proposals/{proposal_id}?reveal=true"
    )
    assert shown_response.status_code == 200, shown_response.text
    shown = shown_response.json()
    assert shown["payload"]["config"]["headers"]["X-Private"] == "header-secret"
    assert shown["payload"]["config"]["env"]["PRIVATE_ENV"] == "env-secret"
    assert shown["payload"]["config"]["auth"]["token"] == "bearer-secret"

    response = await client.patch(f"/api/v1/mcp-proposals/{proposal_id}", json={
        **hidden["payload"],
        "description": "final",
        "mode": "disabled",
        "isolation": "service",
        "config_isolation": None,
    })
    assert response.status_code == 200, response.text
    approved = await client.post(f"/api/v1/mcp-proposals/{proposal_id}/approve", json={})
    assert approved.status_code == 200, approved.text
    server_id = approved.json()["approved_mcp_id"]

    service_hidden = (await client.get(f"/api/v1/mcps/{server_id}")).json()
    assert service_hidden["description"] == "final"
    assert service_hidden["config"]["auth"]["token"] == "[REDACTED]"
    service_shown = await client.get(f"/api/v1/mcps/{server_id}?reveal=true")
    assert service_shown.status_code == 200, service_shown.text
    assert service_shown.json()["config"]["auth"]["token"] == "bearer-secret"

    async with app.state.db.session() as session:
        row = await session.get(McpServer, server_id)
        serialized = json.dumps(row.config)
    assert "header-secret" not in serialized
    assert "env-secret" not in serialized
    assert "bearer-secret" not in serialized

    assert redact({
        "headers": {"X-Private": "header-secret"},
        "env": {"PRIVATE_ENV": "env-secret"},
    }) == {
        "headers": {"X-Private": "[REDACTED]"},
        "env": {"PRIVATE_ENV": "[REDACTED]"},
    }

    rejected_id = await submit({
        "name": "Rejected", "transport": "stdio", "config": {"command": "never"},
    })
    await client.post(f"/api/v1/mcp-proposals/{rejected_id}/reject", json={
        "reason": "not approved",
    })
    agent_view = await list_proposals(app, user)
    rejected = next(item for item in agent_view["items"] if item["id"] == rejected_id)
    assert rejected["rejection_reason"] == "not approved"


@pytest.mark.asyncio
async def test_oauth_validation_test_hint_and_pending_authorization(requirement5_web):
    _app, client, _user, submit = requirement5_web
    proposal_id = await submit({
        "name": "OAuth Pending", "transport": "streamable-http",
        "config": {
            "url": "https://example.test/mcp",
            "auth": {
                "type": "oauth",
                "authorization_url": "https://auth.example.test/authorize",
                "token_url": "https://auth.example.test/token",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "scopes": "tools.read",
            },
        },
    })
    invalid = await client.patch(f"/api/v1/mcp-proposals/{proposal_id}", json={
        "mode": "lazy",
        "isolation": "service",
        "config": {
            "url": "https://example.test/mcp",
            "auth": {
                "type": "oauth",
                "authorization_url": "https://auth.example.test/authorize",
                "token_url": "https://auth.example.test/token",
                "client_id": "",
                "client_secret": "client-secret",
                "scopes": "tools.read",
            },
        },
    })
    assert invalid.status_code == 422
    assert "client_id" in invalid.text

    configured = await client.patch(f"/api/v1/mcp-proposals/{proposal_id}", json={
        "mode": "lazy",
        "isolation": "service",
        "config_isolation": None,
        "config": {
            "url": "https://example.test/mcp",
            "auth": {
                "type": "oauth",
                "authorization_url": "https://auth.example.test/authorize",
                "token_url": "https://auth.example.test/token",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "scopes": "tools.read",
            },
        },
    })
    assert configured.status_code == 200, configured.text
    tested = await client.post(f"/api/v1/mcp-proposals/{proposal_id}/test")
    assert tested.status_code == 409
    assert tested.json()["detail"] == "OAuth 类型需要在审批通过并授权后验证"

    approved = await client.post(f"/api/v1/mcp-proposals/{proposal_id}/approve", json={})
    assert approved.status_code == 200, approved.text
    service_id = approved.json()["approved_mcp_id"]
    service = (await client.get(f"/api/v1/mcps/{service_id}")).json()
    assert service["cache_status"] == "auth_required"
