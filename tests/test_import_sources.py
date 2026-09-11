"""Import through the same preview/edit/deduplicate/commit API used by the console."""
import asyncio
import json

import httpx
import pytest

from mcp_manager.app import create_app
from mcp_manager.config import Settings


@pytest.fixture
async def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, database_url="", secret_key="test-key"))
    started, stop = asyncio.Event(), asyncio.Event()
    async def lifetime():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()
    task = asyncio.create_task(lifetime())
    await asyncio.wait_for(started.wait(), 10)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as web:
            await web.post("/api/v1/auth/register", json={"username": "admin", "password": "password1234"})
            await web.post("/api/v1/auth/login", json={"username": "admin", "password": "password1234"})
            web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
            yield web
    finally:
        stop.set()
        await task


async def preview(client, data, channel="generic"):
    result = await client.post("/api/v1/mcps/import-preview", json={"data": data, "channel": channel})
    assert result.status_code == 200, result.text
    return result.json()


async def test_codex_full_toml_maps_headers_timeouts_and_disabled(client):
    data = await preview(client, '''
model = "unrelated"
[mcp_servers.local]
command = "uv"
args = ["run", "example.py"]
startup_timeout_sec = 45
tool_timeout_sec = 90
enabled = false
[mcp_servers.local.env]
TEST = "literal"
[mcp_servers.remote]
url = "https://example.com/mcp"
bearer_token_env_var = "MCP_TEST_TOKEN"
http_headers = { "X-App" = "demo" }
env_http_headers = { "X-Key" = "MCP_TEST_KEY" }
''', "codex")
    assert data["errors"] == []
    local, remote = data["items"]
    assert local["mode"] == "disabled"
    assert local["config"]["startup_timeout"] == 45
    assert local["config"]["call_timeout"] == 90
    assert remote["transport"] == "streamable-http"
    assert remote["config"]["headers"] == {"X-App": "demo"}
    assert remote["config"]["auth"] == {"type": "bearer", "token_env": "MCP_TEST_TOKEN"}
    assert remote["config"]["env_headers"] == {"X-Key": "MCP_TEST_KEY"}
    assert "model" not in json.dumps(data["items"])


async def test_claude_global_and_project_entries_do_not_silently_overwrite(client):
    data = await preview(client, {"mcpServers": {"echo": {"command": "node"}},
        "projects": {"/work/demo": {"mcpServers": {"echo": {"type": "http", "url": "https://example.com/mcp"}}}}}, "claude")
    assert len(data["items"]) == 2
    assert len({i["slug"] for i in data["items"]}) == 2


async def test_dsh_registry_and_cordis_yaml(client):
    registry = await preview(client, {"version": 1, "entries": [
        {"name": "clock", "transport": "stdio", "command": "uv", "tier": "on-demand", "tools": [{"name": "cached"}]},
        {"name": "remote", "transport": "streamable-http", "url": "https://example.com/mcp", "tier": "disabled"}
    ]}, "dsh")
    assert len(registry["items"]) == 2
    assert registry["items"][0]["mode"] == "lazy"
    assert "tools" not in registry["items"][0]["config"]
    assert registry["items"][1]["mode"] == "disabled"
    patch = await preview(client, '''
- target: root
  insert:
    - name: "@deepseek-ai/dsh-mcp-client"
      disabled: true
      config:
        serverName: demo
        transport: stdio
        command: node
        args: [server.js]
    - name: unrelated-plugin
      config: {}
''', "dsh")
    assert len(patch["items"]) == 1
    assert patch["items"][0]["name"] == "demo"
    assert patch["items"][0]["mode"] == "disabled"


async def test_malformed_entry_is_reported_without_losing_valid_entries(client):
    data = await preview(client, {"mcpServers": {"bad": 42, "good": {"command": "uv"}}})
    assert [x["name"] for x in data["items"]] == ["good"]
    assert data["errors"][0]["name"] == "bad"


async def test_deduplicate_by_config_keeps_different_auth_and_does_not_commit(client):
    raw = {"mcpServers": {"one": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer one"}}}}
    imported = (await client.post("/api/v1/mcps/import", json={"data": raw})).json()
    assert len(imported["results"]) == 1
    pending = [
        {"name": "renamed", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer one"}},
        {"name": "different_auth", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer two"}},
        {"name": "within_batch", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer two"}}
    ]
    response = await client.post("/api/v1/mcps/import-deduplicate", json={"data": pending})
    assert response.status_code == 200, response.text
    assert [x["name"] for x in response.json()["items"]] == ["different_auth"]
    assert len(response.json()["duplicates"]) == 2
    assert (await client.get("/api/v1/mcps")).json()["total"] == 1
    assert "Bearer one" not in json.dumps(response.json()["duplicates"])


async def test_import_can_refresh_cache_as_a_tracked_background_job(client):
    response = await client.post("/api/v1/mcps/import", json={"refresh": True, "data": [
        {"name": "rest", "transport": "rest", "config": {"tools": [
            {"name": "read", "request": {"url": "https://example.com"}}]}},
        {"name": "off", "transport": "stdio", "mode": "disabled", "config": {"command": "must-not-start"}}
    ]})
    assert response.status_code == 200
    body = response.json()
    assert "refresh_job" in body
    assert body["refresh_job"]["total"] == 1
    for _ in range(100):
        job = (await client.get("/api/v1/jobs/" + body["refresh_job"]["id"])).json()
        if job["status"] == "completed":
            break
        await asyncio.sleep(.02)
    assert job["status"] == "completed"
    assert len(body["results"]) == 2


async def test_scan_uses_fixed_channel_paths_and_preserves_raw_file(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    content = '[mcp_servers.demo]\ncommand = "uv"\n'
    (tmp_path / "config.toml").write_text(content, encoding="utf-8", newline="")
    response = await client.get("/api/v1/mcps/import-sources?channel=codex")
    assert response.status_code == 200, response.text
    assert response.json()["sources"][0]["content"] == content
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == content
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/api/v1/mcps/import-sources?channel=../../secret")).status_code == 422
    await client.post("/api/v1/auth/logout")
    assert (await client.get("/api/v1/mcps/import-sources?channel=codex")).status_code == 401
