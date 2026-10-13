"""Exact dispatch uses one indexed definition, not a whole downstream directory."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import types
from starlette.requests import Request
from test_catalog_paged_api import indexed_catalog

from mcp_manager.catalog import alias
from mcp_manager.database import ApiToken


async def gateway_context(harness):
    response = await harness.web.post("/api/v1/tokens", json={
        "name": "exact-tests", "scope_mode": "all", "discovery_mode": "discovery",
    })
    assert response.status_code == 200
    request = Request({
        "type": "http", "method": "POST", "path": "/mcp", "app": harness.app,
        "headers": [(b"authorization", ("Bearer " + response.json()["token"]).encode())],
    })
    return SimpleNamespace(request=request), response.json()["id"]


async def invoke(harness, context, name, arguments):
    return await harness.app.state.gateway.call_tool(
        context, types.CallToolRequestParams(name=name, arguments=arguments)
    )


def downstream(monkeypatch, harness, tools):
    calls = []
    class Connection:
        async def discover(self):
            return {"tools": tools}
        async def call(self, name, arguments):
            calls.append((name, arguments))
            return {"content": [{"type": "text", "text": "ok"}]}
    @asynccontextmanager
    async def connector(spec):
        assert spec.config["command"] == "synthetic-never-launched"
        yield Connection()
    monkeypatch.setattr(harness.app.state.runtime, "connector", connector)
    return calls


async def test_exact_call_never_builds_directory_or_reads_unrelated_catalogs(db_app, monkeypatch):
    row, tools = await indexed_catalog(db_app, 2)
    catalog = db_app.app.state.catalog
    async with db_app.db.locked() as session:
        for index in range(100):
            await catalog.create_in_session(session, {
                "name": f"cold-{index}", "mode": "lazy", "transport": "stdio",
                "config": {"command": "never-launch-an-unrelated-service"},
            })
    context, _identity = await gateway_context(db_app)
    calls = downstream(monkeypatch, db_app, tools)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Exact dispatch must not build a full directory")
    monkeypatch.setattr(db_app.app.state.gateway, "directory", forbidden)
    result = await invoke(db_app, context, "gateway_call", {
        "name": alias(row.slug, tools[0]["name"]), "arguments": {},
    })
    assert not result.is_error, result
    assert calls == [(tools[0]["name"], {})]
    assert all(value["server_id"] == row.id for value in db_app.app.state.runtime.status())


async def test_exact_call_reuses_one_schema_validator(db_app, monkeypatch):
    row, tools = await indexed_catalog(db_app, 1)
    context, _identity = await gateway_context(db_app)
    downstream(monkeypatch, db_app, tools)
    import mcp_manager.catalog as catalog_module

    original, constructed = catalog_module.Draft202012Validator, 0
    def validator(*args, **kwargs):
        nonlocal constructed
        constructed += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(catalog_module, "Draft202012Validator", validator)
    for _ in range(2):
        result = await invoke(db_app, context, "gateway_call", {
            "name": alias(row.slug, tools[0]["name"]), "arguments": {},
        })
        assert not result.is_error, result
    assert constructed <= 1


async def test_revocation_during_exact_definition_read_prevents_dispatch(db_app, monkeypatch):
    row, tools = await indexed_catalog(db_app, 1)
    context, identity = await gateway_context(db_app)
    calls = downstream(monkeypatch, db_app, tools)
    entered, release = asyncio.Event(), asyncio.Event()
    original = db_app.app.state.catalog.artifacts.read_json
    async def delayed(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(db_app.app.state.catalog.artifacts, "read_json", delayed)
    task = asyncio.create_task(invoke(db_app, context, "gateway_call", {
        "name": alias(row.slug, tools[0]["name"]), "arguments": {},
    }))
    try:
        ready = asyncio.create_task(entered.wait())
        done, _pending = await asyncio.wait({ready, task}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        assert ready in done, "Exact route must read its one authoritative definition"
        async with db_app.db.locked() as session:
            (await session.get(ApiToken, identity)).disabled = True
    finally:
        release.set()
        if not ready.done():
            ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)
    result = await asyncio.wait_for(task, 5)
    assert result.is_error and result.structured_content["error"]["code"] == "permission_revoked"
    assert calls == []


async def test_cold_service_search_never_starts_a_lazy_downstream(db_app, monkeypatch):
    catalog = db_app.app.state.catalog
    async with db_app.db.locked() as session:
        await catalog.create_in_session(session, {
            "name": "Cold", "mode": "lazy", "transport": "stdio", "config": {"command": "cold"},
        })
    context, _identity = await gateway_context(db_app)
    starts = 0
    @asynccontextmanager
    async def connector(spec):
        nonlocal starts
        starts += 1
        raise AssertionError("A search must not start a lazy service")
        yield
    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    result = await invoke(db_app, context, "gateway_search_mcps", {"query": "*"})
    assert not result.is_error and starts == 0
    assert result.structured_content["items"][0]["catalog_status"] == "empty"


async def test_indexed_search_constructs_only_returned_page_contracts(db_app, monkeypatch):
    row, tools = await indexed_catalog(db_app, 301)
    context, _identity = await gateway_context(db_app)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Indexed search must not construct a whole directory snapshot")
    monkeypatch.setattr(db_app.app.state.gateway.discovery, "snapshot", forbidden, raising=False)
    original, reads = db_app.app.state.catalog.artifacts.read_json, 0
    async def counted(*args, **kwargs):
        nonlocal reads
        reads += 1
        return await original(*args, **kwargs)
    monkeypatch.setattr(db_app.app.state.catalog.artifacts, "read_json", counted)
    result = await invoke(db_app, context, "gateway_search_tools", {"mcp": row.id, "limit": 1})
    assert not result.is_error, result
    assert result.structured_content["total"] == 301
    assert result.structured_content["tools"][0]["inputSchema"] == tools[0]["inputSchema"]
    assert reads <= 1


async def test_indexed_global_search_and_service_summaries_never_build_all_contracts(db_app, monkeypatch):
    row, tools = await indexed_catalog(db_app, 61)
    context, _identity = await gateway_context(db_app)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Discovery must not build a full contract snapshot")
    monkeypatch.setattr(db_app.app.state.gateway.discovery, "snapshot", forbidden, raising=False)
    original, reads = db_app.app.state.catalog.artifacts.read_json, 0
    async def counted(*args, **kwargs):
        nonlocal reads
        reads += 1
        return await original(*args, **kwargs)
    monkeypatch.setattr(db_app.app.state.catalog.artifacts, "read_json", counted)
    services = await invoke(db_app, context, "gateway_search_mcps", {"query": "*"})
    assert not services.is_error, services
    item = services.structured_content["items"][0]
    assert item["tool_count"] == 61 and item["catalog_status"] == "ready"
    assert len(item["tools_list"]) <= 50 and reads == 0
    assert item["tools_list_truncated"] is True
    result = await invoke(db_app, context, "gateway_search_tools", {"mcp": "*", "limit": 1})
    assert not result.is_error, result
    assert result.structured_content["total"] == 61
    assert result.structured_content["tools"][0]["gateway_name"] == alias(row.slug, tools[0]["name"])
    assert reads <= 1


async def test_indexed_pages_cover_every_schema_and_exact_multi_reports_missing(db_app):
    row, tools = await indexed_catalog(db_app, 7)
    context, _identity = await gateway_context(db_app)
    cursor, names = None, []
    while True:
        result = await invoke(db_app, context, "gateway_search_tools", {
            "mcp": row.id, "limit": 2, **({"cursor": cursor} if cursor else {}),
        })
        assert not result.is_error, result
        page = result.structured_content
        assert page["total"] == 7
        names.extend(tool["gateway_name"] for tool in page["tools"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    assert names == [alias(row.slug, tool["name"]) for tool in tools]
    selected = await invoke(db_app, context, "gateway_search_tools", {
        "mcp": row.id, "tools": [tools[0]["name"], "absent"],
    })
    assert not selected.is_error, selected
    assert selected.structured_content["missing_tools"] == ["absent"]
    assert selected.structured_content["tools"][0]["inputSchema"] == tools[0]["inputSchema"]


async def test_mixed_compatibility_scores_only_metadata_and_reads_one_page_definition(db_app, monkeypatch):
    row, tools = await indexed_catalog(db_app, 7)
    catalog = db_app.app.state.catalog
    async with db_app.db.locked() as session:
        legacy = await catalog.create_in_session(session, {
            "name": "z-legacy", "slug": "z-legacy", "mode": "lazy", "transport": "stdio",
            "config": {"command": "never-launch"},
        })
    catalog.save_cache(legacy, {
        "tools": [{"name": "other", "inputSchema": {"type": "object"}}], "cache_status": "ready",
    })
    context, _identity = await gateway_context(db_app)
    original, reads = catalog.artifacts.read_json, 0
    async def counted(*args, **kwargs):
        nonlocal reads
        reads += 1
        return await original(*args, **kwargs)
    monkeypatch.setattr(catalog.artifacts, "read_json", counted)
    result = await invoke(db_app, context, "gateway_search_tools", {"mcp": "*", "limit": 1})
    assert not result.is_error, result
    assert result.structured_content["total"] == 8
    assert result.structured_content["tools"][0]["gateway_name"] == alias(row.slug, tools[0]["name"])
    assert reads <= 1


async def test_proposal_feature_cannot_be_bypassed_by_guessing_hidden_tool(db_app):
    context, _identity = await gateway_context(db_app)
    result = await invoke(db_app, context, "gateway_mcp_proposals", {"action": "list"})
    assert result.is_error and result.structured_content["error"]["code"] == "forbidden"


async def test_exact_call_does_not_repeatedly_list_all_authorized_service_ids(db_app, monkeypatch):
    from sqlalchemy import event

    row, tools = await indexed_catalog(db_app, 1)
    context, _identity = await gateway_context(db_app)
    downstream(monkeypatch, db_app, tools)
    statements = []
    def trace(connection, cursor, statement, parameters, context, many):
        if ("SELECT mcp_servers.id" in statement and "FROM mcp_servers" in statement
                and "mcp_servers.id =" not in statement):
            statements.append(statement)
    event.listen(db_app.db.engine.sync_engine, "before_cursor_execute", trace)
    try:
        result = await invoke(db_app, context, "gateway_call", {
            "name": alias(row.slug, tools[0]["name"]), "arguments": {},
        })
        assert not result.is_error, result
        assert len(statements) <= 2
    finally:
        event.remove(db_app.db.engine.sync_engine, "before_cursor_execute", trace)


async def test_validator_cache_accounts_for_owned_schema_containers():
    import sys
    from collections import OrderedDict

    from mcp_manager.catalog import Catalog

    catalog = object.__new__(Catalog)
    catalog._validators, catalog._validator_bytes = OrderedDict(), 0
    children = [{"type": "string"} for _ in range(1000)]
    properties = {f"field-{index}": child for index, child in enumerate(children)}
    schema = {"type": "object", "properties": properties}
    await catalog.validate_tool_arguments({"inputSchema": schema}, {})
    minimum_owned = sum(sys.getsizeof(value) for value in [schema, properties, *children, *properties.keys()])
    assert minimum_owned <= catalog._validator_bytes <= 32 * 1024 * 1024


async def test_schema_validation_never_fetches_network_references(monkeypatch):
    import urllib.request
    from collections import OrderedDict
    from io import BytesIO

    from mcp_manager.catalog import Catalog
    from mcp_manager.runtime import GatewayError

    catalog = object.__new__(Catalog)
    catalog._validators, catalog._validator_bytes = OrderedDict(), 0
    fetched = []
    def remote(request):
        fetched.append(request.full_url)
        return BytesIO(b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object"}')
    monkeypatch.setattr(urllib.request, "urlopen", remote)
    with pytest.raises(GatewayError) as caught:
        await catalog.validate_tool_arguments({"inputSchema": {"$ref": "https://example.invalid/schema"}}, {})
    assert caught.value.code == "schema_reference_unavailable"
    assert fetched == []
    local = {"$defs": {"payload": {"type": "object"}}, "$ref": "#/$defs/payload"}
    await catalog.validate_tool_arguments({"inputSchema": local}, {})


async def test_warm_exact_callbacks_do_not_reload_full_configuration_blobs(db_app, monkeypatch):
    import re

    from sqlalchemy import event

    row, tools = await indexed_catalog(db_app, 1)
    context, _identity = await gateway_context(db_app)
    downstream(monkeypatch, db_app, tools)
    arguments = {"name": alias(row.slug, tools[0]["name"]), "arguments": {}}
    assert not (await invoke(db_app, context, "gateway_call", arguments)).is_error
    statements = []
    def trace(connection, cursor, statement, parameters, context, many):
        if re.search(r"mcp_servers\.config(?:\s|,)", statement):
            statements.append(statement)
    event.listen(db_app.db.engine.sync_engine, "before_cursor_execute", trace)
    try:
        result = await invoke(db_app, context, "gateway_call", arguments)
        assert not result.is_error, result
        # One current target configuration for spec/materialization, not every ACL callback.
        assert len(statements) <= 1
    finally:
        event.remove(db_app.db.engine.sync_engine, "before_cursor_execute", trace)
