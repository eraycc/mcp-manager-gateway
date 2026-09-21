from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import ApiToken, McpProposal, McpServer, User
from mcp_manager.gateway import discovery_tools
from mcp_manager.proposals import normalize_proposals, resource_index, submit_proposals


def token(**values):
    defaults = {
        "discovery_mode": "discovery",
        "enable_resource_tools": False,
        "enable_mcp_proposals": False,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_gateway_tool_count_is_token_scoped_and_bounded():
    assert [item["name"] for item in discovery_tools(token())] == [
        "gateway_search_mcps",
        "gateway_search_tools",
        "gateway_call",
    ]
    assert [item["name"] for item in discovery_tools(token(enable_resource_tools=True))][-2:] == [
        "gateway_list_resources",
        "gateway_read_resource",
    ]
    all_tools = discovery_tools(token(enable_resource_tools=True, enable_mcp_proposals=True))
    assert len(all_tools) == 6
    assert all_tools[-1]["name"] == "gateway_propose_mcp"
    assert all_tools[3]["annotations"]["readOnlyHint"] is True
    assert all_tools[4]["annotations"]["readOnlyHint"] is True


def test_proposal_schema_accepts_single_and_batch_but_excludes_approval_fields():
    single = normalize_proposals({
        "name": "Docs",
        "transport": "streamable-http",
        "config": {"url": "https://example.test/mcp", "auth": {"type": "bearer", "token": "secret"}},
        "purpose": "Search internal docs",
        "declared_capabilities": ["tools", "resources"],
        "requested_permissions": ["network"],
    })
    assert single[0]["transport"] == "streamable-http"
    batch = normalize_proposals({"proposals": [
        {"name": "Local", "transport": "stdio", "config": {"command": "uv", "args": ["run", "server.py"]}},
        {"name": "REST", "transport": "rest", "config": {"tools": [{
            "name": "get", "inputSchema": {"type": "object"},
            "request": {"method": "GET", "url": "https://example.test/items"},
            "response": {"type": "json"},
        }]}},
    ]})
    assert len(batch) == 2
    with pytest.raises(ValueError, match="审批"):
        normalize_proposals({
            "name": "Bad", "transport": "stdio", "config": {"command": "x"}, "mode": "eager"
        })
    with pytest.raises(ValueError):
        normalize_proposals({"name": "Bad", "transport": "stdio", "config": {}})


def test_resource_index_filters_and_preserves_provider_and_read_instructions():
    rows = [
        SimpleNamespace(id="one", name="Docs", slug="docs"),
        SimpleNamespace(id="two", name="Other", slug="other"),
    ]
    cache = {
        "one": {
            "resources": [{"uri": "docs://guide", "name": "Guide", "description": "Install guide"}],
            "templates": [{"uriTemplate": "docs://{page}", "name": "Page"}],
            "prompts": [{"name": "summarize", "description": "Summarize a document"}],
        },
        "two": {"resources": [{"uri": "other://x", "name": "Other"}], "templates": [], "prompts": []},
    }
    items = resource_index(rows, lambda row: cache[row.id], mcp="Docs", keyword="guide")
    assert len(items) == 1
    assert items[0]["mcp"] == {"id": "one", "name": "Docs", "slug": "docs"}
    assert items[0]["uri"] == "mcp-manager://one/docs://guide"
    assert items[0]["read_with"] == {
        "tool": "gateway_read_resource",
        "arguments": {"uri": "mcp-manager://one/docs://guide"},
    }


@pytest.mark.asyncio
async def test_admin_proposal_token_approval_and_exact_duplicate_guard(tmp_path):
    app = create_app(Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path}/requirements3.db",
        secret_key="requirements3-test-key",
        public_url="http://test",
    ))
    async with app.router.lifespan_context(app):  # noqa: SIM117
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.post("/api/v1/auth/register", json={
                "username": "admin", "password": "password12345"
            })).status_code == 200
            assert (await client.post("/api/v1/auth/login", json={
                "username": "admin", "password": "password12345"
            })).status_code == 200
            client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]
            created = await client.post("/api/v1/tokens", json={
                "name": "proposal-agent",
                "scope_mode": "all",
                "enable_resource_tools": True,
                "enable_mcp_proposals": True,
            })
            assert created.status_code == 200, created.text
            assert created.json()["enable_resource_tools"] is True
            assert created.json()["enable_mcp_proposals"] is True

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as ordinary:
                await ordinary.post("/api/v1/auth/register", json={
                    "username": "ordinary", "password": "password12345"
                })
                await ordinary.post("/api/v1/auth/login", json={
                    "username": "ordinary", "password": "password12345"
                })
                ordinary.headers["X-CSRF-Token"] = ordinary.cookies["mcp_csrf"]
                resource_only = await ordinary.post("/api/v1/tokens", json={
                    "name": "resources", "enable_resource_tools": True,
                })
                assert resource_only.status_code == 200
                forbidden = await ordinary.post("/api/v1/tokens", json={
                    "name": "proposal", "enable_mcp_proposals": True,
                })
                assert forbidden.status_code == 403

            async with app.state.db.session() as session:
                user = await session.scalar(select(User).where(User.username == "admin"))
                api_token = await session.scalar(select(ApiToken).where(
                    ApiToken.id == created.json()["id"]
                ))
            payload = {
                "name": "Queued",
                "transport": "stdio",
                "config": {"command": "missing-but-disabled"},
                "purpose": "Approval workflow test",
            }
            submitted = await submit_proposals(app, user, api_token, payload)
            proposal_id = submitted["items"][0]["id"]
            configured = await client.patch(
                f"/api/v1/mcp-proposals/{proposal_id}",
                json={"mode": "disabled", "isolation": "service"},
            )
            assert configured.status_code == 200, configured.text
            approved = await client.post(f"/api/v1/mcp-proposals/{proposal_id}/approve", json={})
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "approved"

            submitted = await submit_proposals(app, user, api_token, payload)
            duplicate_id = submitted["items"][0]["id"]
            await client.patch(
                f"/api/v1/mcp-proposals/{duplicate_id}",
                json={"mode": "disabled", "isolation": "service"},
            )
            duplicate = await client.post(
                f"/api/v1/mcp-proposals/{duplicate_id}/approve", json={}
            )
            assert duplicate.status_code == 409
            async with app.state.db.session() as session:
                assert len(list((await session.scalars(select(McpServer))).all())) == 1
                pending = await session.get(McpProposal, duplicate_id)
                assert pending.status == "pending"


def test_api_token_feature_defaults_are_closed():
    assert ApiToken.enable_resource_tools.property.columns[0].default.arg is False
    assert ApiToken.enable_mcp_proposals.property.columns[0].default.arg is False
