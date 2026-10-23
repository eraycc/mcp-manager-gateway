"""Default-off compatibility, destructive disable, and discoverable session snapshots."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from starlette.requests import Request
from test_exact_route_scalability import gateway_context, invoke
from test_resource_compat import enabled_context, resource_catalog

from mcp_manager.catalog import alias
from mcp_manager.database import ApiToken


async def enabled(harness):
    ctx, sid = await enabled_context(harness)
    response = await harness.web.patch("/api/v1/settings", json={"embedded_resource.inject": True})
    assert response.status_code == 200
    return ctx, sid


async def capture(harness, ctx, row, path):
    gateway = harness.app.state.gateway
    user, token, _ = await gateway.principal(ctx.request)
    value = await gateway.capture_embedded(ctx.request, user, token, row, {
        "content": [{"type": "resource", "resource": {
            "uri": "repo://example/repo/contents/" + path, "text": "body:" + path,
        }}],
    })
    return value


async def test_new_install_defaults_off_and_does_not_capture(db_app):
    row, _ = await resource_catalog(db_app)
    ctx, _ = await enabled_context(db_app, inject=False)
    assert (await db_app.web.get("/api/v1/settings")).json()["embedded_resource.inject"] is False
    value = await capture(db_app, ctx, row, "admin/layout.tsx")
    assert len(value.content) == 1
    assert not db_app.app.state.gateway.embedded.items


async def test_disabling_purges_every_session_and_reenable_requires_new_call(db_app):
    row, _ = await resource_catalog(db_app)
    ctx, _ = await enabled(db_app)
    other, _ = await enabled(db_app)
    await capture(db_app, ctx, row, "first.tsx")
    await capture(db_app, other, row, "second.tsx")
    gateway = db_app.app.state.gateway
    old_uris = list(gateway.embedded.items)
    assert len(old_uris) == 2
    response = await db_app.web.patch("/api/v1/settings", json={"embedded_resource.inject": False})
    assert response.status_code == 200
    assert not gateway.embedded.items and gateway.embedded.bytes == 0
    value = await capture(db_app, ctx, row, "disabled.tsx")
    assert len(value.content) == 1 and not gateway.embedded.items
    await db_app.web.patch("/api/v1/settings", json={"embedded_resource.inject": True})
    assert not gateway.embedded.items
    for uri in old_uris:
        assert (await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri})).is_error
    await capture(db_app, ctx, row, "fresh.tsx")
    assert len(gateway.embedded.items) == 1
    assert next(iter(gateway.embedded.items)) not in old_uris


async def test_disabled_while_capture_waits_does_not_repopulate_after_reenable(db_app):
    row, _ = await resource_catalog(db_app)
    ctx, _ = await enabled(db_app)
    gateway = db_app.app.state.gateway
    user, token, _ = await gateway.principal(ctx.request)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def authorize():
        entered.set()
        await finish.wait()
    pending = asyncio.create_task(gateway.capture_embedded(ctx.request, user, token, row, {
        "content": [{"type": "resource", "resource": {"uri": "fixture://stale", "text": "body"}}],
    }, authorize=authorize))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await db_app.web.patch("/api/v1/settings", json={"embedded_resource.inject": False})
        await db_app.web.patch("/api/v1/settings", json={"embedded_resource.inject": True})
    finally:
        finish.set()
    value = await asyncio.wait_for(pending, 5)
    assert len(value.content) == 1
    assert not gateway.embedded.items


async def test_newest_temporary_resources_lead_every_page_path(db_app):
    row, tools = await resource_catalog(db_app)
    catalog = db_app.app.state.catalog
    scope = await catalog.index_scope(row)
    generation = await catalog.index.begin_refresh(scope)
    await catalog.index.stage_page(generation.id, "tools", tools)
    await catalog.index.stage_page(generation.id, "resources", [
        {"uri": f"fixture://static/{index:03}", "name": f"static-{index:03}", "description": "layout"}
        for index in range(65)
    ])
    await catalog.index.publish(generation.id, expected_scope=scope)
    ctx, _ = await enabled(db_app)
    await capture(db_app, ctx, row, "old/layout.tsx")
    await capture(db_app, ctx, row, "admin/layout.tsx")
    old, new = list(db_app.app.state.gateway.embedded.items)
    for mcp in (row.id, row.slug, row.name):
        page = await invoke(db_app, ctx, "gateway_list_resources", {"mcp": mcp, "limit": 1})
        assert not page.is_error
        assert page.structured_content["items"][0]["uri"] == new
    for query in ("layout.tsx", "admin/layout.tsx", "admin layout tsx"):
        page = await invoke(db_app, ctx, "gateway_list_resources", {
            "mcp": row.id, "keyword": query, "match": "keyword_only", "limit": 1,
        })
        assert not page.is_error and page.structured_content["items"][0]["uri"] == new
    page = await invoke(db_app, ctx, "gateway_list_resources", {"keyword": "layout", "limit": 1})
    assert page.structured_content["items"][0]["uri"] == new
    cursor, seen = None, []
    while True:
        page = await invoke(db_app, ctx, "gateway_list_resources", {
            "mcp": row.id, "limit": 7, **({"cursor": cursor} if cursor else {}),
        })
        assert not page.is_error, page
        body = page.structured_content
        seen.extend(item["uri"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert seen[:2] == [new, old] and len(seen) == len(set(seen)) == 67
    cursor, seen = None, []
    while True:
        page = await db_app.app.state.gateway.list_resources(ctx, SimpleNamespace(cursor=cursor))
        seen.extend(str(item.uri) for item in page.resources)
        cursor = page.next_cursor
        if not cursor:
            break
    assert seen[:2] == [new, old] and len(seen) == len(set(seen)) == 67


async def test_implicit_retention_lease_captures_without_session_header(db_app, monkeypatch):
    row, tools = await resource_catalog(db_app)
    ctx, token_id = await gateway_context(db_app)
    async with db_app.db.locked() as session:
        (await session.get(ApiToken, token_id)).enable_resource_tools = True
    await db_app.web.patch("/api/v1/settings", json={"embedded_resource.inject": True})
    class Connection:
        async def call(self, name, arguments):
            return {"content": [{"type": "resource", "resource": {
                "uri": "repo://example/repo/contents/admin/layout.tsx", "text": "implicit body",
            }}]}
    @asynccontextmanager
    async def connector(spec):
        yield Connection()
    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    result = await invoke(db_app, ctx, "gateway_call", {"name": alias(row.slug, tools[0]["name"]), "arguments": {}})
    assert not result.is_error
    assert any("Temporary embedded resource:" in getattr(block, "text", "") for block in result.content)
    # A different HTTP request with the same authenticated implicit lease.
    again = SimpleNamespace(request=Request(dict(ctx.request.scope)))
    page = await invoke(db_app, again, "gateway_list_resources", {
        "mcp": row.id, "keyword": "admin/layout.tsx", "limit": 1,
    })
    assert not page.is_error and page.structured_content["returned"] == 1
    uri = page.structured_content["items"][0]["uri"]
    read = await invoke(db_app, again, "gateway_read_resource", {"uri": uri})
    assert read.structured_content["contents"][0]["text"] == "implicit body"
    other, _ = await enabled(db_app)
    assert (await invoke(db_app, other, "gateway_read_resource", {"uri": uri})).is_error
    user, token, _ = await db_app.app.state.gateway.principal(ctx.request)
    lease = db_app.app.state.gateway.lease(ctx.request, user, token)
    await db_app.app.state.gateway.disconnect_lease(lease.id)
    assert not db_app.app.state.gateway.embedded.items


async def test_legacy_catalog_also_lists_newest_temporary_resource_first(db_app):
    from test_resource_search_revocation import resource_fixture

    row, _token, ctx = await resource_fixture(db_app)
    await db_app.web.patch("/api/v1/settings", json={"embedded_resource.inject": True})
    gateway = db_app.app.state.gateway
    user, token, _ = await gateway.principal(ctx.request)
    gateway.lease(ctx.request, user, token)
    await capture(db_app, ctx, row, "old/layout.tsx")
    await capture(db_app, ctx, row, "admin/layout.tsx")
    old, new = list(gateway.embedded.items)
    result = await invoke(db_app, ctx, "gateway_list_resources", {"mcp": row.id, "limit": 1})
    assert not result.is_error and result.structured_content["items"][0]["uri"] == new
    result = await invoke(db_app, ctx, "gateway_list_resources", {
        "mcp": row.id, "keyword": "admin layout tsx", "match": "keyword_only",
    })
    assert [item["uri"] for item in result.structured_content["items"]] == [new]
    native = await gateway.list_resources(ctx, SimpleNamespace(cursor=None))
    assert [str(item.uri) for item in native.resources[:2]] == [new, old]


async def test_mcp_filter_does_not_include_another_providers_temporary_resources(db_app):
    from mcp_manager.database import McpServer

    row, _ = await resource_catalog(db_app)
    catalog = db_app.app.state.catalog
    other = await catalog.create({
        "name": "Other provider", "slug": "other-provider", "mode": "disabled",
        "transport": "stdio", "config": {"command": "synthetic-never-launched"},
    })
    async with db_app.db.locked() as session:
        (await session.get(McpServer, other.id)).mode = "lazy"
    other = await catalog.get(other.id)
    ctx, _ = await enabled(db_app)
    await capture(db_app, ctx, row, "first.tsx")
    await capture(db_app, ctx, other, "second.tsx")
    for provider, name in [(row, "first.tsx"), (other, "second.tsx")]:
        result = await invoke(db_app, ctx, "gateway_list_resources", {"mcp": provider.id, "limit": 1})
        assert not result.is_error
        item = result.structured_content["items"][0]
        assert item["name"] == name and item["mcp"]["id"] == provider.id and item["temporary"]
