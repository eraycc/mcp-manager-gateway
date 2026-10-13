"""Budget the actual JSON-RPC result, including text + structured duplicate content."""
from types import SimpleNamespace

from mcp import types
from test_catalog_paged_api import indexed_catalog
from test_exact_route_scalability import gateway_context, invoke

from mcp_manager.database import ApiToken


def wire_bytes(result):
    message = types.JSONRPCResponse(
        jsonrpc="2.0", id=17, result=result.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    return len(message.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


async def wide_catalog(harness, *, count=8, text=None):
    row, _tools = await indexed_catalog(harness, 0)
    catalog = harness.app.state.catalog
    scope = await catalog.index_scope(row)
    generation = await catalog.index.begin_refresh(scope)
    tools = [{"name": f"wide-{index:03}", "inputSchema": {
        "type": "object", "description": text or ('中\n\\"' * 30000),
        "properties": {"value": {"type": "string"}}, "additionalProperties": False,
    }} for index in range(count)]
    await catalog.index.stage_page(generation.id, "tools", tools)
    await catalog.index.publish(generation.id, expected_scope=scope)
    return row, tools


async def test_whole_mcp_envelope_budget_keeps_full_schemas_and_traversal(db_app):
    row, tools = await wide_catalog(db_app)
    context, _identity = await gateway_context(db_app)
    cursor, seen = None, []
    while True:
        result = await invoke(db_app, context, "gateway_search_tools", {
            "mcp": row.id, "limit": 8, **({"cursor": cursor} if cursor else {}),
        })
        assert not result.is_error, result
        assert wire_bytes(result) <= 1048576
        page = result.structured_content
        for item in page["tools"]:
            expected = tools[int(item["gateway_name"].rsplit("-", 1)[1])]
            assert item["inputSchema"] == expected["inputSchema"]
            seen.append(item["gateway_name"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 8


async def test_single_schema_too_large_for_double_envelope_is_explicit_error(db_app):
    row, _tools = await wide_catalog(db_app, count=1, text="x" * 600000)
    context, _identity = await gateway_context(db_app)
    result = await invoke(db_app, context, "gateway_search_tools", {"mcp": row.id})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "catalog_entry_too_large"
    assert wire_bytes(result) <= 1048576


async def test_native_tools_are_paged_and_schema_complete_without_legacy_directory(db_app, monkeypatch):
    _row, tools = await wide_catalog(db_app, count=8, text="x" * 180000)
    context, identity = await gateway_context(db_app)
    async with db_app.db.locked() as session:
        (await session.get(ApiToken, identity)).discovery_mode = "native"
    async def forbidden(*args, **kwargs):
        raise AssertionError("Native listing must use indexed page references")
    monkeypatch.setattr(db_app.app.state.gateway, "directory", forbidden)
    cursor, seen = None, []
    while True:
        page = await db_app.app.state.gateway.list_tools(context, SimpleNamespace(cursor=cursor))
        assert wire_bytes(page) <= 1048576
        for item in page.tools:
            original = tools[int(item.name.rsplit("-", 1)[1])]
            assert item.input_schema == original["inputSchema"]
            seen.append(item.name)
        cursor = page.next_cursor
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 8


async def test_name_only_pages_do_not_read_schema_artifacts(db_app, monkeypatch):
    row, tools = await indexed_catalog(db_app, 61)
    context, _identity = await gateway_context(db_app)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Name pages must not read or build full Schema contracts")
    monkeypatch.setattr(db_app.app.state.catalog.artifacts, "read_json", forbidden)
    cursor, seen = None, []
    while True:
        result = await invoke(db_app, context, "gateway_search_tools", {
            "mcp": row.id, "names_only": True, "limit": 20, **({"cursor": cursor} if cursor else {}),
        })
        assert not result.is_error, result
        assert wire_bytes(result) <= 1048576
        page = result.structured_content
        assert page["total"] == 61
        assert all("inputSchema" not in item for item in page["tools"])
        seen.extend(item["name"] for item in page["tools"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    assert seen == [item["name"] for item in tools]


async def test_native_resource_prompt_and_template_pages_are_complete(db_app, monkeypatch):
    row, _tools = await indexed_catalog(db_app, 0)
    catalog = db_app.app.state.catalog
    scope = await catalog.index_scope(row)
    generation = await catalog.index.begin_refresh(scope)
    expected = {
        "resources": [{"name": f"resource-{index}", "uri": f"fixture://objects/{index}",
                       "description": "x" * 25000} for index in range(61)],
        "prompts": [{"name": f"prompt-{index}", "description": "x" * 25000,
                     "arguments": [{"name": "value", "required": True}]} for index in range(61)],
        "templates": [{"name": f"template-{index}", "uriTemplate": f"fixture://objects/{index}/{{key}}",
                       "description": "x" * 25000} for index in range(61)],
    }
    for kind, values in expected.items():
        await catalog.index.stage_page(generation.id, kind, values)
    await catalog.index.publish(generation.id, expected_scope=scope)
    context, _identity = await gateway_context(db_app)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Native capabilities must never build a legacy tool directory")
    monkeypatch.setattr(db_app.app.state.gateway, "directory", forbidden)
    for kind, method, field in [
        ("resources", "list_resources", "resources"),
        ("prompts", "list_prompts", "prompts"),
        ("templates", "list_templates", "resource_templates"),
    ]:
        cursor, seen = None, []
        while True:
            page = await getattr(db_app.app.state.gateway, method)(context, SimpleNamespace(cursor=cursor))
            assert wire_bytes(page) <= 1048576
            items = getattr(page, field)
            assert all(item.description == "x" * 25000 for item in items)
            seen.extend(item.name for item in items)
            cursor = page.next_cursor
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == len(expected[kind])


async def test_resource_meta_pages_use_index_and_are_cursor_bound(db_app, monkeypatch):
    row, _tools = await indexed_catalog(db_app, 0)
    catalog = db_app.app.state.catalog
    scope = await catalog.index_scope(row)
    generation = await catalog.index.begin_refresh(scope)
    await catalog.index.stage_page(generation.id, "resources", [
        {"name": f"r-{i}", "uri": f"fixture:///{i}", "description": "x" * 1500} for i in range(61)
    ])
    await catalog.index.publish(generation.id, expected_scope=scope)
    context, token_id = await gateway_context(db_app)
    async with db_app.db.locked() as session:
        (await session.get(ApiToken, token_id)).enable_resource_tools = True
    async def forbidden(*args, **kwargs):
        raise AssertionError("Resource search must not build tool directory or definitions")
    monkeypatch.setattr(db_app.app.state.gateway, "directory", forbidden)
    monkeypatch.setattr(catalog.artifacts, "read_json", forbidden)
    first = await invoke(db_app, context, "gateway_list_resources", {"mcp": row.id, "limit": 20})
    assert not first.is_error, first
    page = first.structured_content
    assert page["total"] == 61 and len(page["items"]) == 20 and wire_bytes(first) <= 1048576
    cursor, seen = page["next_cursor"], [v["uri"] for v in page["items"]]
    changed = await invoke(db_app, context, "gateway_list_resources", {"mcp": row.id, "limit": 21, "cursor": cursor})
    assert changed.is_error and changed.structured_content["error"]["code"] == "catalog_changed"
    while cursor:
        result = await invoke(db_app, context, "gateway_list_resources", {"mcp": row.id, "limit": 20, "cursor": cursor})
        assert not result.is_error, result
        seen.extend(v["uri"] for v in result.structured_content["items"])
        cursor = result.structured_content.get("next_cursor")
    assert len(seen) == len(set(seen)) == 61


async def test_native_prompt_exact_call_uses_index_not_tool_directory(db_app, monkeypatch):
    row, _tools = await indexed_catalog(db_app, 0)
    catalog = db_app.app.state.catalog
    scope = await catalog.index_scope(row)
    generation = await catalog.index.begin_refresh(scope)
    await catalog.index.stage_page(generation.id, "prompts", [{"name": "example", "arguments": []}])
    await catalog.index.publish(generation.id, expected_scope=scope)
    context, _identity = await gateway_context(db_app)
    from mcp_manager.catalog import alias
    async def forbidden(*args, **kwargs):
        raise AssertionError("Exact prompt must not inspect unrelated tools")
    monkeypatch.setattr(db_app.app.state.gateway, "directory", forbidden)
    calls = []
    async def perform(spec, lease, method, name, arguments, **kwargs):
        await kwargs["authorize"]()
        calls.append((method, name, arguments))
        return {"messages": [{"role": "user", "content": {"type": "text", "text": "hello"}}]}
    monkeypatch.setattr(db_app.app.state.runtime, "perform", perform)
    result = await db_app.app.state.gateway.get_prompt(context, SimpleNamespace(name=alias(row.slug, "example"), arguments={}))
    assert result.messages[0].content.text == "hello"
    assert calls == [("get_prompt", "example", {})]


def test_page_builder_rejects_impossible_empty_cursor_and_no_progress():
    import pytest

    from mcp_manager.paging import McpPageBuilder
    from mcp_manager.runtime import GatewayError
    kwargs = {"limit": 20, "version": "fixed", "signing_key": "test-only", "envelope": lambda page: page}
    with pytest.raises(GatewayError, match="outside"):
        McpPageBuilder(total=0, offset=1, **kwargs)
    with pytest.raises(GatewayError, match="disappeared"):
        McpPageBuilder(total=1, offset=0, **kwargs).finish()
