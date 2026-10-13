"""An in-flight semantic query must fail closed after permissions change."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from mcp_manager.database import McpServer
from mcp_manager.runtime import GatewayError


async def resource_fixture(harness):
    state = harness.app.state
    row = await state.catalog.create({
        "name": "Resource service", "transport": "stdio", "mode": "disabled",
        "config": {"command": "synthetic-never-launched"},
    })
    async with harness.db.locked() as session:
        (await session.get(McpServer, row.id)).mode = "lazy"
    row = await state.catalog.get(row.id)
    state.catalog.save_cache(row, {
        "tools": [], "resources": [{"name": "fixture-resource", "description": "Useful description",
            "uri": "fixture://private/SYNTHETIC_METADATA_MARKER", "extra": "private-extension"}],
        "prompts": [], "templates": [], "cache_status": "ready", "cache_at": "synthetic",
    })
    token = (await harness.web.post("/api/v1/tokens", json={
        "name": "resource", "scope_mode": "all", "discovery_mode": "discovery", "enable_resource_tools": True
    })).json()
    configured = await harness.web.patch("/api/v1/settings/embedding", json={
        "enabled": True, "base_url": "https://example.invalid", "model": "synthetic-model"
    })
    assert configured.status_code == 200
    request = SimpleNamespace(app=harness.app, headers={"Authorization": "Bearer " + token["token"]})
    return row, token, SimpleNamespace(request=request)


async def test_revocation_during_embedding_discloses_no_resource(db_app, monkeypatch):
    _row, token, ctx = await resource_fixture(db_app)
    entered, release = asyncio.Event(), asyncio.Event()
    async def scores(scope, documents, query, config, **kwargs):
        entered.set()
        await release.wait()
        return {item["id"]: 0.9 for item in documents}, {"mode": "hybrid"}
    monkeypatch.setattr(db_app.app.state.embeddings, "scores", scores)
    params = SimpleNamespace(name="gateway_list_resources", arguments={"keyword": "fixture"})
    task = asyncio.create_task(db_app.app.state.gateway._call_tool(ctx, params))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        response = await db_app.web.patch("/api/v1/tokens/" + token["id"], json={"disabled": True})
        assert response.status_code == 200
    finally:
        release.set()
    with pytest.raises(GatewayError) as caught:
        await asyncio.wait_for(task, 5)
    assert caught.value.code == "permission_revoked"


async def test_embedding_documents_exclude_uris_and_extension_fields(db_app, monkeypatch):
    _row, _token, ctx = await resource_fixture(db_app)
    texts = []
    async def scores(scope, documents, query, config, **kwargs):
        texts.extend(document["text"] for document in documents)
        return {item["id"]: 0.9 for item in documents}, {"mode": "hybrid"}
    monkeypatch.setattr(db_app.app.state.embeddings, "scores", scores)
    result = await db_app.app.state.gateway._call_tool(ctx, SimpleNamespace(
        name="gateway_list_resources", arguments={"keyword": "fixture"}
    ))
    assert texts and all("SYNTHETIC_METADATA_MARKER" not in text for text in texts)
    assert all("private-extension" not in text for text in texts)
    assert json.loads(result.content[0].text)["count"] == 1


async def test_configuration_change_during_embedding_discloses_no_resource(db_app, monkeypatch):
    row, _token, ctx = await resource_fixture(db_app)
    entered, release = asyncio.Event(), asyncio.Event()
    async def scores(scope, documents, query, config, **kwargs):
        entered.set()
        await release.wait()
        return {item["id"]: 0.9 for item in documents}, {"mode": "hybrid"}
    monkeypatch.setattr(db_app.app.state.embeddings, "scores", scores)
    task = asyncio.create_task(db_app.app.state.gateway._call_tool(ctx, SimpleNamespace(
        name="gateway_list_resources", arguments={"keyword": "fixture"}
    )))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with db_app.db.locked() as session:
            (await session.get(McpServer, row.id)).revision += 1
    finally:
        release.set()
    with pytest.raises(GatewayError) as caught:
        await asyncio.wait_for(task, 5)
    assert caught.value.code == "permission_revoked"
