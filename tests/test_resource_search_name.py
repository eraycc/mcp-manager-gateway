"""Resource search is progressive, token-gated, and advertised under its new name."""
from types import SimpleNamespace

from test_exact_route_scalability import gateway_context, invoke
from test_resource_compat import enabled_context, resource_catalog


async def test_token_checkbox_controls_search_and_read_pair(db_app):
    ctx, identity = await gateway_context(db_app)
    gateway = db_app.app.state.gateway
    before = await gateway.list_tools(ctx, SimpleNamespace(cursor=None))
    assert [tool.name for tool in before.tools] == [
        "gateway_search_mcps", "gateway_search_tools", "gateway_call",
    ]
    response = await db_app.web.patch("/api/v1/tokens/" + identity, json={"enable_resource_tools": True})
    assert response.status_code == 200 and response.json()["enable_resource_tools"] is True
    after = await gateway.list_tools(ctx, SimpleNamespace(cursor=None))
    assert [tool.name for tool in after.tools] == [
        "gateway_search_mcps", "gateway_search_tools", "gateway_call",
        "gateway_search_resources", "gateway_read_resource",
    ]
    schema = after.tools[3].input_schema
    assert set(schema["properties"]) == {"mcp", "kind", "keyword", "match", "uri_prefix", "limit", "cursor"}
    assert schema["properties"]["kind"]["enum"] == ["resource", "prompt", "template", "all"]
    assert schema["properties"]["match"]["enum"] == ["exact", "fuzzy", "keyword_only"]


async def test_search_resources_paginates_metadata_and_rejects_removed_name(db_app):
    row, _tools = await resource_catalog(db_app)
    ctx, _ = await enabled_context(db_app)
    query = {"mcp": row.id, "kind": "resource", "limit": 1}
    first = await invoke(db_app, ctx, "gateway_search_resources", query)
    assert not first.is_error, first
    body = first.structured_content
    assert body["total"] == 2 and body["items"][0]["name"] == "health"
    assert body["next_cursor"]
    assert body["items"][0]["read_with"]["tool"] == "gateway_read_resource"
    assert "text" not in body["items"][0] and "contents" not in body["items"][0]
    second = await invoke(db_app, ctx, "gateway_search_resources", {**query, "cursor": body["next_cursor"]})
    assert not second.is_error
    assert second.structured_content["items"][0]["name"] == "weather"
    assert second.structured_content["next_cursor"] is None
    exact = await invoke(db_app, ctx, "gateway_search_resources", {
        "mcp": row.id, "kind": "prompt", "keyword": "health", "match": "exact",
    })
    assert not exact.is_error
    assert len(exact.structured_content["items"]) == 1
    assert exact.structured_content["items"][0]["kind"] == "prompt"
    invalid = await invoke(db_app, ctx, "gateway_search_resources", {"kind": "tools"})
    assert invalid.is_error
    assert invalid.structured_content["error"]["code"] == "invalid_arguments"
    assert invalid.structured_content["error"]["tool"] == "gateway_search_resources"
    removed = await invoke(db_app, ctx, "gateway_list_resources", {})
    assert removed.is_error


async def test_resource_search_and_read_remain_unavailable_without_token_flag(db_app):
    await resource_catalog(db_app)
    ctx, _ = await gateway_context(db_app)
    assert (await invoke(db_app, ctx, "gateway_search_resources", {})).is_error
    assert (await invoke(db_app, ctx, "gateway_read_resource", {"uri": "fixture://private"})).is_error


async def test_empty_resource_search_uses_default_page_and_reads_only_metadata(db_app, monkeypatch):
    row, tools = await resource_catalog(db_app)
    catalog = db_app.app.state.catalog
    scope = await catalog.index_scope(row)
    generation = await catalog.index.begin_refresh(scope)
    await catalog.index.stage_page(generation.id, "tools", tools)
    await catalog.index.stage_page(generation.id, "resources", [
        {"uri": f"fixture://resource-{index:03}", "name": f"resource-{index:03}"}
        for index in range(65)
    ])
    await catalog.index.publish(generation.id, expected_scope=scope)
    ctx, _ = await enabled_context(db_app)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Resource search must not read bodies or start a downstream")

    monkeypatch.setattr(catalog.artifacts, "read_json", forbidden)
    monkeypatch.setattr(db_app.app.state.runtime, "perform", forbidden)
    arguments, names, page_sizes = {}, [], []
    for _ in range(4):
        result = await invoke(db_app, ctx, "gateway_search_resources", arguments)
        assert not result.is_error, result
        page = result.structured_content
        assert page["total"] == 65 and page["returned"] == len(page["items"])
        page_sizes.append(page["returned"])
        names.extend(item["name"] for item in page["items"])
        assert all(not {"text", "blob", "contents"} & item.keys() for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        arguments = {"cursor": cursor}
    assert page_sizes == [20, 20, 20, 5]
    assert names == [f"resource-{index:03}" for index in range(65)]
    assert cursor is None
