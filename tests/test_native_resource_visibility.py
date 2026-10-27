"""Native list visibility is independent of token tools and read authorization."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import types
from test_exact_route_scalability import gateway_context, invoke
from test_resource_compat import enabled_context, resource_catalog

from mcp_manager.catalog import alias
from mcp_manager.database import ApiToken


async def test_native_visibility_defaults_on_and_requires_boolean(db_app):
    saved = (await db_app.web.get("/api/v1/settings")).json()
    assert saved.get("exposeNativeResources") is True
    for invalid in ["false", 0, 1, None, []]:
        response = await db_app.web.patch("/api/v1/settings", json={"exposeNativeResources": invalid})
        assert response.status_code == 422
    response = await db_app.web.patch("/api/v1/settings", json={"exposeNativeResources": False})
    assert response.status_code == 200
    assert (await db_app.web.get("/api/v1/settings")).json()["exposeNativeResources"] is False


@pytest.mark.parametrize("exposed", [True, False])
@pytest.mark.parametrize("resource_tools", [True, False])
async def test_native_list_and_token_tools_matrix(db_app, exposed, resource_tools):
    await resource_catalog(db_app)
    ctx, token_id = await gateway_context(db_app)
    async with db_app.db.locked() as session:
        (await session.get(ApiToken, token_id)).enable_resource_tools = resource_tools
    response = await db_app.web.patch("/api/v1/settings", json={"exposeNativeResources": exposed})
    assert response.status_code == 200
    gateway = db_app.app.state.gateway
    for method, field in [(gateway.list_resources, "resources"), (gateway.list_templates, "resource_templates"),
                          (gateway.list_prompts, "prompts")]:
        page = await method(ctx, SimpleNamespace(cursor=None))
        assert bool(getattr(page, field)) is exposed
        assert page.next_cursor is None
    tools = await gateway.list_tools(ctx, SimpleNamespace(cursor=None))
    names = {tool.name for tool in tools.tools}
    assert ("gateway_search_resources" in names) is resource_tools
    assert ("gateway_read_resource" in names) is resource_tools
    assert db_app.app.state.runtime.status() == []


@pytest.mark.parametrize("resource_tools", [True, False])
async def test_hidden_lists_keep_search_snapshots_and_known_reads(db_app, monkeypatch, resource_tools):
    row, _ = await resource_catalog(db_app)
    ctx, _sid = await enabled_context(db_app)
    gateway = db_app.app.state.gateway
    response = await db_app.web.patch("/api/v1/settings", json={"exposeNativeResources": False})
    assert response.status_code == 200
    user, token, _ = await gateway.principal(ctx.request)
    async with db_app.db.locked() as session:
        (await session.get(ApiToken, token.id)).enable_resource_tools = resource_tools
    await gateway.capture_embedded(ctx.request, user, token, row, {
        "content": [{"type": "resource", "resource": {"uri": "fixture://snapshot", "text": "snapshot body"}}],
    })
    uri = next(iter(gateway.embedded.items))
    page = await gateway.list_resources(ctx, SimpleNamespace(cursor="previous-page"))
    assert page.resources == [] and page.next_cursor is None
    if resource_tools:
        searched = await invoke(db_app, ctx, "gateway_search_resources", {"kind": "resource"})
        assert not searched.is_error
        assert uri in {item["uri"] for item in searched.structured_content["items"]}
        read = await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri})
        assert read.structured_content["contents"][0]["text"] == "snapshot body"
    native = await gateway.read_resource(ctx, SimpleNamespace(uri=uri))
    assert native.contents[0].text == "snapshot body"

    class Connection:
        async def read_resource(self, original):
            return {"contents": [{"uri": original, "text": "static body"}]}

        async def get_prompt(self, name, arguments):
            return {"messages": [{"role": "user", "content": {"type": "text", "text": "prompt body"}}]}

    @asynccontextmanager
    async def connector(spec):
        yield Connection()

    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    native = await gateway.read_resource(ctx, SimpleNamespace(uri="mcp-manager://" + row.id + "/fixture://health"))
    assert native.contents[0].text == "static body"
    prompt = await gateway.get_prompt(ctx, SimpleNamespace(name=alias(row.slug, "health"), arguments={}))
    assert prompt.messages[0].content.text == "prompt body"
    response = await db_app.web.patch("/api/v1/settings", json={"exposeNativeResources": True})
    assert response.status_code == 200
    page = await gateway.list_resources(ctx, SimpleNamespace(cursor=None))
    assert uri in {str(item.uri) for item in page.resources}


async def test_disable_during_paging_cannot_return_visible_resources(db_app, monkeypatch):
    await resource_catalog(db_app)
    ctx, _ = await gateway_context(db_app)
    gateway = db_app.app.state.gateway
    original = gateway.revalidate_search_scope

    async def disable(request, expected):
        response = await db_app.web.patch("/api/v1/settings", json={"exposeNativeResources": False})
        assert response.status_code == 200
        return await original(request, expected)

    monkeypatch.setattr(gateway, "revalidate_search_scope", disable)
    page = await gateway.list_resources(ctx, types.PaginatedRequestParams())
    assert page.resources == [] and page.next_cursor is None


async def test_only_saved_visibility_changes_notify_clients(db_app, monkeypatch):
    changed = AsyncMock()
    monkeypatch.setattr(db_app.app.state.gateway.resource_notifications, "changed", changed)
    for payload, expected_count in [
        ({"exposeNativeResources": True}, 0),
        ({"exposeNativeResources": False}, 1),
        ({"exposeNativeResources": False}, 1),
        ({"title": "Independent setting"}, 1),
        ({"exposeNativeResources": True}, 2),
    ]:
        response = await db_app.web.patch("/api/v1/settings", json=payload)
        assert response.status_code == 200
        assert changed.await_count == expected_count
    response = await db_app.web.patch("/api/v1/settings", json={"exposeNativeResources": "false"})
    assert response.status_code == 422
    assert changed.await_count == 2
