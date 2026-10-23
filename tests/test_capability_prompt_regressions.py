"""Exact web definitions and prompt compatibility reads share authorized routing."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from sqlalchemy import select
from test_catalog_paged_api import indexed_catalog
from test_exact_route_scalability import gateway_context, invoke

from mcp_manager.database import ApiToken


@pytest.fixture
async def prompt_catalog(db_app, monkeypatch):
    state = db_app.app.state
    row, _ = await indexed_catalog(db_app, 0)
    scope = await state.catalog.index_scope(row)
    generation = await state.catalog.index.begin_refresh(scope)
    await state.catalog.index.stage_page(generation.id, "resources", [
        {"name": "Health", "uri": "device://health"},
        {"name": "Real prompt-scheme resource", "uri": f"prompt://{row.slug}/real"},
    ])
    await state.catalog.index.stage_page(generation.id, "templates", [
        {"name": "Device", "uriTemplate": "device://sensor/{key}"},
    ])
    await state.catalog.index.stage_page(generation.id, "prompts", [
        {"name": "issue_to_fix_workflow", "arguments": [{"name": "topic"}]},
        {"name": "required", "arguments": [{"name": "topic", "required": True}]},
        {"name": "real"},
    ])
    await state.catalog.index.publish(generation.id, expected_scope=scope)
    calls = []

    class Peer:
        async def read_resource(self, uri):
            calls.append(("resource", uri))
            return {"contents": [{"uri": uri, "text": "resource", "mimeType": "text/plain"}]}

        async def get_prompt(self, name, arguments):
            calls.append(("prompt", name, arguments))
            return {"description": "Workflow", "messages": [
                {"role": "user", "content": {"type": "text", "text": arguments.get("topic", "workflow")}},
            ]}

    @asynccontextmanager
    async def connector(spec):
        yield Peer()

    monkeypatch.setattr(state.runtime, "connector", connector)
    ctx, token_id = await gateway_context(db_app)
    async with db_app.db.locked() as session:
        (await session.get(ApiToken, token_id)).enable_resource_tools = True
    return row, ctx, calls


@pytest.mark.parametrize("kind,key", [
    ("resources", "uri"), ("templates", "uriTemplate"), ("prompts", "name"),
])
async def test_definition_endpoint_accepts_exact_capability_identifier(db_app, prompt_catalog, kind, key):
    row, _, _ = prompt_catalog
    root = f"/api/v1/mcps/{row.id}/capabilities"
    page = await db_app.web.get(root, params={"kind": kind})
    assert page.status_code == 200, page.text
    entry = page.json()["items"][0]
    result = await db_app.web.get(f"{root}/{kind}/{quote(entry[key], safe='')}")
    assert result.status_code == 200, result.text
    assert result.json()[key] == entry[key]
    assert db_app.app.state.runtime.status() == []


async def test_prompt_resource_bridge_native_and_tool(db_app, prompt_catalog):
    row, ctx, calls = prompt_catalog
    uri = f"mcp-manager://{row.id}/prompt://{row.slug}/issue_to_fix_workflow"
    listing = await invoke(db_app, ctx, "gateway_search_resources", {})
    prompt = next(item for item in listing.structured_content["items"]
                  if item["kind"] == "prompt" and item["name"] == "issue_to_fix_workflow")
    assert prompt["uri"] == uri
    assert prompt["read_with"] == {"tool": "gateway_read_resource", "arguments": {"uri": uri}}
    native = await db_app.app.state.gateway.read_resource(ctx, SimpleNamespace(uri=uri))
    assert str(native.contents[0].uri) == uri
    assert json.loads(native.contents[0].text)["messages"][0]["content"]["text"] == "workflow"
    result = await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri, "arguments": {"topic": "中文"}})
    assert not result.is_error, result
    assert result.structured_content["messages"][0]["content"]["text"] == "中文"
    assert json.loads(result.structured_content["contents"][0]["text"])["messages"][0]["content"]["text"] == "中文"
    assert calls == [("prompt", "issue_to_fix_workflow", {}), ("prompt", "issue_to_fix_workflow", {"topic": "中文"})]


@pytest.mark.parametrize("name,arguments,code", [
    ("missing", {}, "prompt_not_found"),
    ("required", {}, "invalid_arguments"),
    ("issue_to_fix_workflow", {"typo": "x"}, "invalid_arguments"),
])
async def test_prompt_bridge_rejects_invalid_requests_before_dispatch(db_app, prompt_catalog, name, arguments, code):
    row, ctx, calls = prompt_catalog
    uri = f"mcp-manager://{row.id}/prompt://{row.slug}/{name}"
    result = await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri, "arguments": arguments})
    assert result.is_error
    assert result.structured_content["error"]["code"] == code
    assert calls == []


async def test_real_prompt_scheme_resource_keeps_resource_routing(db_app, prompt_catalog):
    row, ctx, calls = prompt_catalog
    original = f"prompt://{row.slug}/real"
    uri = f"mcp-manager://{row.id}/{original}"
    result = await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri})
    assert not result.is_error, result
    assert result.structured_content["contents"][0]["text"] == "resource"
    assert calls == [("resource", original)]


async def test_native_prompt_error_exposes_actionable_mcp_error(db_app, prompt_catalog):
    from mcp.shared.exceptions import MCPError

    row, ctx, calls = prompt_catalog
    uri = f"mcp-manager://{row.id}/prompt://{row.slug}/required"
    with pytest.raises(MCPError) as error:
        await db_app.app.state.gateway.read_resource(ctx, SimpleNamespace(uri=uri))
    assert error.value.data["code"] == "invalid_arguments"
    assert "topic" in error.value.message
    assert calls == []


async def test_prompt_bridge_over_real_gateway_http(db_app, prompt_catalog):
    import asyncio
    import socket

    import httpx2
    import uvicorn
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.exceptions import MCPError

    row, ctx, _ = prompt_catalog
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = "http://127.0.0.1:" + str(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(db_app.app, lifespan="off", log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(300):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.01)
        assert server.started
        async with httpx2.AsyncClient(headers=dict(ctx.request.headers), trust_env=False) as http:  # noqa: SIM117 - MCP client depends on HTTP context
            async with Client(streamable_http_client(url + "/mcp", http_client=http), cache=None) as client:
                uri = f"mcp-manager://{row.id}/prompt://{row.slug}/issue_to_fix_workflow"
                value = await client.read_resource(uri)
                assert json.loads(value.contents[0].text)["messages"][0]["content"]["text"] == "workflow"
                result = await client.call_tool("gateway_read_resource", {"uri": uri, "arguments": {"topic": "wire"}})
                assert not result.is_error, result
                assert result.structured_content["messages"][0]["content"]["text"] == "wire"
                with pytest.raises(MCPError) as error:
                    await client.read_resource(f"mcp-manager://{row.id}/prompt://{row.slug}/required")
                assert error.value.data["code"] == "invalid_arguments"
                assert "topic" in error.value.message
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


async def test_prompt_bridge_revocation_before_dispatch(db_app, prompt_catalog, monkeypatch):
    import asyncio

    row, ctx, calls = prompt_catalog
    state = db_app.app.state
    entered, release = asyncio.Event(), asyncio.Event()
    perform = state.runtime.perform

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await perform(*args, **kwargs)

    monkeypatch.setattr(state.runtime, "perform", delayed)
    uri = f"mcp-manager://{row.id}/prompt://{row.slug}/issue_to_fix_workflow"
    task = asyncio.create_task(invoke(db_app, ctx, "gateway_read_resource", {"uri": uri}))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with db_app.db.locked() as session:
            tokens = (await session.scalars(select(ApiToken))).all()
            for token in tokens:
                token.disabled = True
    finally:
        release.set()
    result = await asyncio.wait_for(task, 5)
    assert result.is_error
    assert calls == []


async def test_prompt_bridge_loads_only_one_exact_definition(db_app, prompt_catalog, monkeypatch):
    row, ctx, _ = prompt_catalog
    artifacts = db_app.app.state.catalog.artifacts
    read = artifacts.read_json
    reads = []

    async def counted(*args, **kwargs):
        reads.append(args[0].id)
        return await read(*args, **kwargs)

    monkeypatch.setattr(artifacts, "read_json", counted)
    uri = f"mcp-manager://{row.id}/prompt://{row.slug}/issue_to_fix_workflow"
    value = await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri})
    assert not value.is_error, value
    assert len(reads) == 1
    reads.clear()
    uri = f"mcp-manager://{row.id}/prompt://{row.slug}/real"
    value = await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri})
    assert not value.is_error, value
    assert reads == []
