"""Regressions reported in docs/bugs/新问题.log."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_exact_route_scalability import gateway_context, invoke

from mcp_manager.catalog import alias
from mcp_manager.database import ApiToken, McpServer
from mcp_manager.imports import normalize_import


async def rest_service(harness, monkeypatch):
    from rest_fixture import rest_connect
    monkeypatch.setattr(harness.app.state.runtime, "connector", rest_connect)
    response = await harness.web.post("/api/v1/mcps", json={
        "name": "Roundtrip", "slug": "roundtrip", "transport": "rest",
        "description": "完整导出", "tags": ["regression"], "mode": "lazy",
        "config": {"tools": [{"name": "echo", "inputSchema": {"type": "object"},
                             "request": {"url": "https://example.test/echo"}}],
                   "call_timeout": 42},
    })
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("action,mode", [("lazy", "lazy"), ("enable", "eager"), ("disable", "disabled")])
async def test_batch_policy_persists_success_result(db_app, monkeypatch, action, mode):
    row = await rest_service(db_app, monkeypatch)
    response = await db_app.web.post("/api/v1/mcps/batch", json={"ids": [row["id"]], "action": action})
    assert response.status_code == 200, response.text
    identity = response.json()["id"]
    future = db_app.app.state.jobs.tasks.get(identity)
    if future:
        await asyncio.wait_for(asyncio.shield(future), 10)
    status = await db_app.app.state.jobs.store.get_status(identity)
    assert status["status"] == "completed", status
    assert status["succeeded"] == 1
    result = await db_app.app.state.jobs.store.get_result(identity, 0)
    assert result["result"]["mode"] == mode
    assert isinstance(result["result"]["created_at"], str)


async def test_copy_populates_indexed_capability_directory(db_app, monkeypatch):
    row = await rest_service(db_app, monkeypatch)
    response = await db_app.web.post(f'/api/v1/mcps/{row["id"]}/copy')
    assert response.status_code == 200, response.text
    copied = response.json()
    page = await db_app.web.get(f'/api/v1/mcps/{copied["id"]}/capabilities')
    assert page.json()["total"] == 1, page.text
    assert page.json()["items"][0]["name"] == "echo"


async def test_standard_export_and_mmg_metadata_roundtrip(db_app, monkeypatch):
    row = await rest_service(db_app, monkeypatch)
    response = await db_app.web.post("/api/v1/mcps/export", json={"ids": [row["id"]]})
    assert response.status_code == 200, response.text
    entry = response.json()["mcpServers"][row["slug"]]
    assert entry["type"] == "rest"
    assert entry["tools"] == row["config"]["tools"]
    assert entry["config"]["format"] == "mmg-v1"
    assert "tools" not in entry["config"]
    parsed = normalize_import(response.json())
    assert parsed["errors"] == []
    item = parsed["items"][0]
    for key in ("name", "slug", "description", "tags", "transport", "mode", "isolation", "config"):
        assert item[key] == row[key]


async def test_standard_stdio_and_http_export_extension_import():
    parsed = normalize_import({"mcpServers": {
        "local": {"command": "npx", "args": ["-y", "example"], "env": {"VAR": "example"},
                  "config": {"format": "mmg-v1", "name": "Local", "transport": "stdio",
                             "description": "local", "tags": ["test"], "mode": "eager",
                             "isolation": "session", "encoding": "utf-8", "startup_timeout": 35}},
        "remote": {"type": "http", "url": "https://example.test/mcp", "headers": {"X-Test": "value"},
                   "config": {"format": "mmg-v1", "transport": "streamable-http", "mode": "lazy",
                              "isolation": "user", "verify_tls": True}},
    }})
    assert parsed["errors"] == [], parsed
    local, remote = parsed["items"]
    assert local["config"]["command"] == "npx"
    assert local["config"]["startup_timeout"] == 35
    assert local["isolation"] == "session"
    assert remote["transport"] == "streamable-http"
    assert remote["config"]["url"] == "https://example.test/mcp"
    assert "format" not in local["config"]


async def test_resource_proxy_loads_exact_target_config_and_preserves_nested_uri(db_app, monkeypatch):
    state = db_app.app.state
    row = await state.catalog.create({
        "name": "Devices", "slug": "devices", "mode": "disabled", "transport": "stdio",
        "config": {"command": "synthetic-resource-peer"},
    })
    async with db_app.db.locked() as session:
        (await session.get(McpServer, row.id)).mode = "lazy"
    row = await state.catalog.get(row.id)
    scope = await state.catalog.index_scope(row)
    generation = await state.catalog.index.begin_refresh(scope)
    await state.catalog.index.stage_page(generation.id, "resources", [
        {"name": "Health", "uri": "device://health"},
    ])
    await state.catalog.index.stage_page(generation.id, "templates", [
        {"name": "Device", "uriTemplate": "device://{key}"},
    ])
    await state.catalog.index.stage_page(generation.id, "prompts", [
        {"name": "status", "arguments": [{"name": "key", "required": True}]},
    ])
    await state.catalog.index.publish(generation.id, expected_scope=scope)
    calls = []

    class Peer:
        async def read_resource(self, uri):
            calls.append(uri)
            return {"contents": [{"uri": uri, "text": "healthy", "mimeType": "text/plain"}]}

        async def get_prompt(self, name, arguments):
            assert name == "status" and arguments == {"key": "health"}
            return {"messages": [{"role": "user", "content": {"type": "text", "text": "health"}}]}

    @asynccontextmanager
    async def connector(spec):
        assert spec.config["command"] == "synthetic-resource-peer"
        yield Peer()

    monkeypatch.setattr(state.runtime, "connector", connector)
    ctx, token_id = await gateway_context(db_app)
    async with db_app.db.locked() as session:
        (await session.get(ApiToken, token_id)).enable_resource_tools = True
    uri = f"mcp-manager://{row.id}/device://health"
    native = await state.gateway.read_resource(ctx, SimpleNamespace(uri=uri))
    assert str(native.contents[0].uri) == uri
    result = await invoke(db_app, ctx, "gateway_read_resource", {"uri": uri})
    assert not result.is_error, result
    assert result.structured_content["contents"][0]["text"] == "healthy"
    assert calls == ["device://health", "device://health"]
    templates = await state.gateway.list_templates(ctx, SimpleNamespace(cursor=None))
    assert templates.resource_templates[0].uri_template == f"mcp-manager://{row.id}/device://{{key}}"
    prompt = await state.gateway.get_prompt(ctx, SimpleNamespace(name=alias(row.slug, "status"), arguments={"key": "health"}))
    assert prompt.messages[0].content.text == "health"

async def test_legacy_cache_prompts_refresh_without_starting_downstream(db_app):
    row = await db_app.app.state.catalog.create({
        "name": "Legacy", "mode": "disabled", "transport": "stdio", "config": {"command": "never"},
    })
    async with db_app.db.locked() as session:
        (await session.get(McpServer, row.id)).mode = "lazy"
    row = await db_app.app.state.catalog.get(row.id)
    db_app.app.state.catalog.save_cache(row, {
        "tools": [{"name": "old-tool"}], "cache_status": "ready", "cache_at": "old",
    })
    response = await db_app.web.get(f"/api/v1/mcps/{row.id}/capabilities")
    assert response.status_code == 200
    assert response.json()["refresh_required"] is True
    assert db_app.app.state.runtime.status() == []
