"""Discovery preserves all valid schemas and rechecks permission before remote batches."""
import asyncio
import json

import httpx
import pytest
from test_embeddings import config
from test_progressive_discovery import discovery_env, payload  # noqa: F401

from mcp_manager.embeddings import EmbeddingIndex
from mcp_manager.runtime import GatewayError


async def test_boolean_property_schema_remains_discoverable(discovery_env):  # noqa: F811
    app, rows, _calls, _ctx, invoke, _web = discovery_env
    cache = app.state.catalog.cached(rows["files"])
    schema = {"type": "object", "properties": {"payload": True}, "required": ["payload"]}
    cache["tools"][0]["inputSchema"] = schema
    app.state.catalog.save_cache(rows["files"], cache)
    assert payload(await invoke("gateway_search_mcps", {}))["total"] == 3
    result = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "read_file"}))
    assert result["tools"][0]["inputSchema"] == schema
    assert result["tools"][0]["invocation"] == {
        "template": {"name": "files__read_file", "arguments": {"payload": "<value>"}},
    }


async def test_revocation_during_service_embedding_prevents_tool_embedding(discovery_env):  # noqa: F811
    app, rows, _calls, ctx, invoke, web = discovery_env
    _user, token, _ids = await app.state.gateway.principal(ctx.request)
    await web.patch("/api/v1/settings/embedding", json=config())
    sent = []

    async def handler(request):
        inputs = json.loads(request.content)["input"]
        sent.append(inputs)
        response = await web.patch("/api/v1/tokens/" + token.id, json={
            "mcp_ids": [rows["archive"].id, rows["empty"].id]})
        assert response.status_code == 200
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(inputs))]})

    app.state.embeddings._transport = httpx.MockTransport(handler)
    result = await invoke("gateway_search_tools", {"mcp": "file storage service", "tool": "read contents"})
    assert result.is_error
    assert len(sent) == 1, sent
    service_documents = "\n".join(sent[0])
    assert "read_file" in service_documents
    assert "File location to read" not in service_documents
    assert "destination email address" not in service_documents


@pytest.mark.parametrize("phase", ["lock", "batch"])
async def test_embedding_authorization_changes_abort_without_keyword_fallback(tmp_path, phase):
    index = EmbeddingIndex(tmp_path)
    allowed = True
    sent = []

    async def authorize():
        if not allowed:
            raise GatewayError("permission_revoked", "Permission revoked")

    async def handler(request):
        nonlocal allowed
        inputs = json.loads(request.content)["input"]
        sent.append(inputs)
        allowed = False
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [1.0, 0.5]} for i in range(len(inputs))]})

    index._transport = httpx.MockTransport(handler)
    docs = [{"id": str(i), "text": "document " + str(i)} for i in range(40)]
    lock = index._lock("http://embedding.test/v1/embeddings", "fixture-model", "scope")
    if phase == "lock":
        await lock.acquire()
    task = asyncio.create_task(index.scores("scope", docs, "query", config(), authorize=authorize))
    if phase == "lock":
        await asyncio.sleep(0)
        allowed = False
        lock.release()
    with pytest.raises(GatewayError, match="Permission revoked"):
        await task
    assert len(sent) == (0 if phase == "lock" else 1)
