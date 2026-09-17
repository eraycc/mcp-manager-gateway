"""Discovery contract regressions; only the downstream network is a fixture."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import types
from starlette.requests import Request

from test_bug1_catalog import console

SCHEMA = {
    "type": "object",
    "$defs": {"options": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1}},
                         "additionalProperties": False}},
    "properties": {"path": {"type": "string", "description": "File location to read"},
                   "options": {"$ref": "#/$defs/options"}},
    "required": ["path"], "additionalProperties": False,
    "examples": [{"path": "example.txt", "options": {"limit": 2}}],
}


@asynccontextmanager
async def discovery_context(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        calls = []
        class Connection:
            def __init__(self, slug):
                self.slug = slug

            async def discover(self):
                return {"tools": [] if self.slug == "empty" else [
                    {"name": "read_file", "description": "Read the contents of a document", "inputSchema": SCHEMA},
                    {"name": "send_message", "title": "Notification sender",
                     "description": "Send a notification",
                     "inputSchema": {"type": "object", "properties": {"recipient": {
                         "type": "string", "description": "destination email address"}},
                         "required": ["recipient"]},
                     "outputSchema": {"type": "object", "properties": {"sent": {"type": "boolean"}}},
                     "annotations": {"readOnlyHint": False}}],
                    "resources": [], "templates": [], "prompts": []}

            async def call(self, name, arguments):
                calls.append((self.slug, name, arguments))
                return {"content": [{"type": "text", "text": self.slug + ":" + name}],
                        "structuredContent": {"server": self.slug, "arguments": arguments}}
        @asynccontextmanager
        async def connect(spec):
            yield Connection(spec.config["command"])
        app.state.runtime.connector = connect
        rows = {}
        for slug, description in [("files", "File storage"), ("archive", ""),
                                  ("empty", ""), ("private", "Hidden administration")]:
            rows[slug] = await app.state.catalog.create(
                {"name": slug, "slug": slug, "description": description,
                 "config": {"command": slug}})
        response = await web.post("/api/v1/tokens", json={
            "name": "discovery", "scope_mode": "selected",
            "mcp_ids": [rows[s].id for s in ("files", "archive", "empty")], "discovery_mode": "discovery"})
        assert response.status_code == 200
        token = response.json()["token"]
        request = Request({"type": "http", "method": "POST", "path": "/mcp", "app": app,
                           "headers": [(b"authorization", ("Bearer " + token).encode())]})
        ctx = SimpleNamespace(request=request)
        async def invoke(name, arguments):
            return await app.state.gateway.call_tool(ctx, types.CallToolRequestParams(name=name, arguments=arguments))
        yield app, rows, calls, ctx, invoke, web


@pytest.fixture
async def discovery_env(tmp_path):
    # MCP SDK task groups must enter and exit in the same owning task.
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    async def own():
        try:
            async with discovery_context(tmp_path) as value:
                ready.set_result(value)
                await stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise
    task = asyncio.create_task(own())
    try:
        yield await ready
    finally:
        stop.set()
        await task


def payload(result):
    assert not result.is_error, result
    body = json.loads(result.content[0].text)
    assert body == result.structured_content
    return body


async def test_services_are_separate_from_tools_and_wildcard_is_complete(discovery_env):
    app, rows, calls, ctx, invoke, web = discovery_env
    initial = await app.state.gateway.list_tools(ctx, None)
    assert {t.name for t in initial.tools} == {
        "gateway_search_mcps", "gateway_search_tools", "gateway_call"}
    body = payload(await invoke("gateway_search_mcps", {"query": "*"}))
    assert {x["slug"] for x in body["items"]} == {"files", "archive", "empty"}
    assert body["total"] == 3 and body["next_cursor"] is None
    by_slug = {item["slug"]: item for item in body["items"]}
    assert by_slug["files"]["tools_list"] == ["read_file", "send_message"]
    assert by_slug["files"]["tool_count"] == len(by_slug["files"]["tools_list"])
    assert by_slug["files"]["description"] == "File storage"
    assert by_slug["files"]["description_source"] == "configured"
    assert by_slug["archive"]["tools_list"] == ["read_file", "send_message"]
    assert by_slug["archive"]["description_source"] == "generated"
    assert "Send a notification" in by_slug["archive"]["description"]
    assert len(by_slug["archive"]["description"]) <= 480
    assert by_slug["empty"]["tools_list"] == []
    assert by_slug["empty"]["description"] == ""
    assert by_slug["empty"]["description_source"] == "missing"
    assert all("tools" not in x and "inputSchema" not in x and "config" not in x for x in body["items"])
    assert all(all(isinstance(name, str) for name in x["tools_list"]) for x in body["items"])
    assert (await app.state.catalog.get(rows["archive"].id)).description == ""
    assert calls == []
    assert app.state.runtime.status() == []


async def test_search_tools_declares_exact_multi_selector(discovery_env):
    app, *_ = discovery_env
    listing = await app.state.gateway.list_tools(discovery_env[3], None)
    schema = next(tool for tool in listing.tools if tool.name == "gateway_search_tools").input_schema

    assert schema["properties"]["tools"] == {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 128},
        "minItems": 1,
        "maxItems": 50,
        "uniqueItems": True,
        "description": "Exact original tool names or gateway_names to disclose together.",
    }


async def test_generated_service_description_and_tool_names_support_l1_search(discovery_env):
    *_, invoke, web = discovery_env
    body = payload(await invoke("gateway_search_mcps", {"query": "notification"}))
    assert "archive" in {item["slug"] for item in body["items"]}
    serialized = json.dumps(body["items"], ensure_ascii=False)
    assert "inputSchema" not in serialized and "invocation" not in serialized


@pytest.mark.parametrize("mcp,tool,want", [
    ("files", "", {"files__read_file", "files__send_message"}),
    ("files", "*", {"files__read_file", "files__send_message"}),
    ("files", "read_file", {"files__read_file"}),
    ("*", "read_file", {"files__read_file", "archive__read_file"}),
    ("", "read_file", {"files__read_file", "archive__read_file"}),
    ("*", "*", {"files__read_file", "files__send_message", "archive__read_file", "archive__send_message"}),
    ("", "", {"files__read_file", "files__send_message", "archive__read_file", "archive__send_message"}),
    ("missing_service", "", set()),
])
async def test_tool_scope_combinations(discovery_env, mcp, tool, want):
    *_, invoke, web = discovery_env
    body = payload(await invoke("gateway_search_tools", {"mcp": mcp, "tool": tool}))
    assert {x["gateway_name"] for x in body["tools"]} == want


async def test_exact_multi_tool_query_is_compact_and_reports_missing(discovery_env):
    app, rows, _calls, _ctx, invoke, _web = discovery_env

    async def unexpected_embeddings(*_args, **_kwargs):
        raise AssertionError("exact service and tools selectors must bypass embeddings")

    app.state.embeddings.scores = unexpected_embeddings
    body = payload(await invoke("gateway_search_tools", {
        "mcp": "files",
        "tools": ["read_file", "send_message", "missing"],
    }))

    assert [tool["gateway_name"] for tool in body["tools"]] == [
        "files__read_file",
        "files__send_message",
    ]
    assert body["missing_tools"] == ["missing"]
    assert body["mcps"] == [{"id": rows["files"].id, "name": "files", "slug": "files"}]
    assert set(body) == {
        "instructions", "mcps", "tools", "truncated", "returned", "total", "missing_tools",
    }
    assert body["truncated"] is False
    assert body["returned"] == body["total"] == 2

    forbidden = {
        "name", "original_name", "mcp_id", "mcp_name", "mcp_slug",
        "catalog_status", "match",
    }
    assert all(not (forbidden & set(tool)) for tool in body["tools"])
    assert "invocation" not in body["tools"][1]
    assert "examples" not in body["tools"][1]


async def test_exact_multi_tool_query_keeps_cross_service_matches(discovery_env):
    *_, invoke, _web = discovery_env
    body = payload(await invoke("gateway_search_tools", {
        "mcp": "*",
        "tools": ["read_file"],
    }))

    assert [tool["gateway_name"] for tool in body["tools"]] == [
        "archive__read_file",
        "files__read_file",
    ]
    assert "missing_tools" not in body


async def test_empty_schema_is_preserved_without_empty_metadata(discovery_env):
    app, rows, _calls, _ctx, invoke, _web = discovery_env
    cache = app.state.catalog.cached(rows["files"])
    app.state.catalog.save_cache(rows["files"], {
        **cache,
        "tools": [*cache["tools"], {
            "name": "status",
            "description": "",
            "inputSchema": {},
            "annotations": {},
        }],
    })

    body = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "status"}))
    assert body["tools"] == [{
        "gateway_name": "files__status",
        "inputSchema": {},
    }]


async def test_complex_schema_keeps_examples_and_required_only_template(discovery_env):
    *_, invoke, _web = discovery_env
    body = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "read_file"}))

    assert body["tools"][0]["examples"] == [{
        "name": "files__read_file",
        "arguments": {"path": "example.txt", "options": {"limit": 2}},
    }]
    assert body["tools"][0]["invocation"] == {
        "template": {
            "name": "files__read_file",
            "arguments": {"path": "<string>"},
        },
    }


async def test_template_skips_false_union_branch(discovery_env):
    app, rows, _calls, _ctx, invoke, _web = discovery_env
    cache = app.state.catalog.cached(rows["files"])
    cache["tools"][0]["inputSchema"] = {
        "anyOf": [False, {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }],
    }
    app.state.catalog.save_cache(rows["files"], cache)

    body = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "read_file"}))
    assert body["tools"][0]["invocation"]["template"]["arguments"] == {"value": "<string>"}


async def test_template_merges_all_allof_requirements(discovery_env):
    app, rows, _calls, _ctx, invoke, _web = discovery_env
    cache = app.state.catalog.cached(rows["files"])
    cache["tools"][0]["inputSchema"] = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "allOf": [{"required": ["a"]}, {"required": ["b"]}],
    }
    app.state.catalog.save_cache(rows["files"], cache)

    body = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "read_file"}))
    assert body["tools"][0]["invocation"]["template"]["arguments"] == {
        "a": "<string>",
        "b": "<integer>",
    }


async def test_template_recursively_merges_overlapping_allof_properties(discovery_env):
    app, rows, _calls, _ctx, invoke, _web = discovery_env
    cache = app.state.catalog.cached(rows["files"])
    cache["tools"][0]["inputSchema"] = {
        "type": "object",
        "allOf": [
            {
                "properties": {"cfg": {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "required": ["a"],
                }},
                "required": ["cfg"],
            },
            {
                "properties": {"cfg": {
                    "type": "object",
                    "properties": {"b": {"type": "integer"}},
                    "required": ["b"],
                }},
                "required": ["cfg"],
            },
        ],
    }
    app.state.catalog.save_cache(rows["files"], cache)

    body = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "read_file"}))
    assert body["tools"][0]["invocation"]["template"]["arguments"] == {
        "cfg": {"a": "<string>", "b": "<integer>"},
    }


async def test_conditional_schema_gets_a_required_branch_template(discovery_env):
    app, rows, _calls, _ctx, invoke, _web = discovery_env
    cache = app.state.catalog.cached(rows["files"])
    cache["tools"][0]["inputSchema"] = {
        "type": "object",
        "properties": {"kind": {"type": "string"}, "value": {"type": "string"}},
        "if": {"properties": {"kind": {"const": "text"}}, "required": ["kind"]},
        "then": {"required": ["value"]},
    }
    app.state.catalog.save_cache(rows["files"], cache)

    body = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "read_file"}))
    assert body["tools"][0]["invocation"]["template"]["arguments"] == {
        "kind": "<string>",
        "value": "<string>",
    }


async def test_full_schema_examples_and_exact_execution(discovery_env):
    app, rows, calls, ctx, invoke, web = discovery_env
    body = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "read_file"}))
    item = body["tools"][0]
    assert item["inputSchema"] == SCHEMA
    assert body["mcps"] == [{"id": rows["files"].id, "name": "files", "slug": "files"}]
    assert item["examples"][0]["arguments"] == {"path": "example.txt", "options": {"limit": 2}}
    result = await invoke("gateway_call", item["examples"][0])
    assert not result.is_error
    assert calls == [("files", "read_file", {"path": "example.txt", "options": {"limit": 2}})]


async def test_invalid_nested_arguments_report_path_without_execution(discovery_env):
    app, rows, calls, ctx, invoke, web = discovery_env
    result = await invoke("gateway_call", {"name": "files__read_file",
                                           "arguments": {"path": "x", "options": {"limit": "wrong"}}})
    assert result.is_error
    error = result.structured_content["error"]
    assert error["code"] == "invalid_arguments"
    assert error["path"] == "/options/limit"
    assert error["expected"] == "integer"
    assert error["recovery"]
    assert calls == []
    assert json.loads(result.content[0].text) == result.structured_content


async def test_field_search_typos_and_unknown_scope_do_not_expand(discovery_env):
    *_, invoke, web = discovery_env
    assert payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "recipent"}))["tools"][0][
        "gateway_name"] == "files__send_message"
    assert payload(await invoke("gateway_search_tools", {"mcp": "private", "tool": "*"}))["tools"] == []
    bad = await invoke("gateway_call", {"name": "read_file", "arguments": {"path": "x"}})
    assert bad.is_error
    assert "private" not in bad.content[0].text


async def test_pagination_has_no_duplicates_and_rejects_changed_catalog(discovery_env):
    app, rows, calls, ctx, invoke, web = discovery_env
    first = payload(await invoke("gateway_search_tools", {"limit": 1}))
    assert first["truncated"] is True
    assert first["next_cursor"]
    assert "tool" in first["hint"]
    names = [first["tools"][0]["gateway_name"]]
    cursor = first["next_cursor"]
    while cursor:
        page = payload(await invoke("gateway_search_tools", {"limit": 1, "cursor": cursor}))
        names.extend(t["gateway_name"] for t in page["tools"])
        cursor = page.get("next_cursor")
    assert page["truncated"] is False
    assert "next_cursor" not in page and "hint" not in page
    assert len(names) == len(set(names)) == 4
    assert {m["slug"] for m in first["mcps"]} == {"archive", "empty", "files"}
    cache = app.state.catalog.cached(rows["files"])
    app.state.catalog.save_cache(rows["files"], {**cache, "tools": cache["tools"][:1]})
    stale = await invoke("gateway_search_tools", {"limit": 1, "cursor": first["next_cursor"]})
    assert stale.is_error
    assert stale.structured_content["error"]["code"] == "catalog_changed"


@pytest.mark.parametrize("arguments", [
    {"mcp": []},
    {"tool": None},
    {"tools": []},
    {"tools": ["read_file", "read_file"]},
    {"tools": [1]},
    {"tools": ["x" * 129]},
    {"tool": "read_file", "tools": ["read_file"]},
    {"limit": 0},
    {"limit": True},
    {"cursor": "invalid"},
    {"unknown": "*"},
])
async def test_bad_discovery_parameters_are_tool_errors(discovery_env, arguments):
    *_, invoke, web = discovery_env
    result = await invoke("gateway_search_tools", arguments)
    assert result.is_error


async def test_legacy_discovery_entry_points_remain_callable(discovery_env):
    *_, invoke, web = discovery_env
    result = await invoke("gateway_search", {"query": "*"})
    assert not result.is_error
    assert len(json.loads(result.content[0].text)) == 4
    inspected = await invoke("gateway_inspect", {"name": "files__read_file"})
    assert json.loads(inspected.content[0].text)["inputSchema"] == SCHEMA
