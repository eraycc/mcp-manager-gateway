"""Call logs keep downstream results and only the gateway's added response blocks."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager

import pytest
from test_exact_route_scalability import invoke
from test_resource_compat import enabled_context, resource_catalog

from mcp_manager.catalog import alias
from mcp_manager.database import ApiToken


async def fixture_call(harness, monkeypatch, *, enabled=True, large=False, during_call=None):
    row, tools = await resource_catalog(harness)
    ctx, token_id = await enabled_context(harness, inject=enabled)
    raw = {"content": [
        {"type": "text", "text": "Temporary embedded resource: UPSTREAM_STATUS_MARKER"},
        {"type": "resource", "resource": {
            "uri": "repo://example/repo/contents/admin/layout.tsx", "mimeType": "text/plain",
            "text": "UPSTREAM_BODY_MARKER" * (6000 if large else 1),
        }},
    ], "structuredContent": {"upstream": True}}
    class Connection:
        async def call(self, name, arguments):
            if during_call:
                await during_call(token_id)
            return copy.deepcopy(raw)
    @asynccontextmanager
    async def connector(spec):
        yield Connection()
    monkeypatch.setattr(harness.app.state.runtime, "connector", connector)
    async def call():
        return await invoke(harness, ctx, "gateway_call", {
            "name": alias(row.slug, tools[0]["name"]), "arguments": {},
        })
    return row, ctx, raw, call


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("large", [False, True])
async def test_call_log_records_only_actual_gateway_additions(db_app, monkeypatch, enabled, large):
    row, _ctx, raw, call = await fixture_call(db_app, monkeypatch, enabled=enabled, large=large)
    result = await call()
    assert not result.is_error, result
    assert result.content[1].resource.text == raw["content"][1]["resource"]["text"]
    logs = db_app.app.state.logs
    page = await logs.query(mcp_id=row.id)
    assert page["total"] == 1
    identity = page["items"][0]["id"]
    response = await db_app.web.get("/api/v1/logs/" + identity)
    assert response.status_code == 200
    saved = response.json()
    assert saved["result"] == raw
    if enabled:
        added = [item.model_dump(mode="json", by_alias=True, exclude_none=True)
                 for item in result.content[len(raw["content"]):]]
        assert len(added) == 1
        assert saved["gateway_processing"] == {"added_content": added}
        delta = json.dumps(saved["gateway_processing"])
        assert "UPSTREAM_BODY_MARKER" not in delta and "UPSTREAM_STATUS_MARKER" not in delta
        uri = next(iter(db_app.app.state.gateway.embedded.items))
        assert uri in added[0]["text"]
    else:
        assert len(result.content) == len(raw["content"])
        assert "gateway_processing" not in saved
    # Rebuild/download/export must preserve the field without creating another logical call.
    await logs.rebuild()
    assert (await logs.detail(identity))["result"] == raw
    assert (await logs.detail(identity)).get("gateway_processing") == saved.get("gateway_processing")
    download = await db_app.web.get("/api/v1/logs/" + identity + "/download")
    assert download.status_code == 200
    assert download.json().get("gateway_processing") == saved.get("gateway_processing")
    exported, _cursor = await logs.export_batch({"mcp_id": row.id})
    assert len(exported) == 1 and exported[0]["result"] == raw
    assert exported[0].get("gateway_processing") == saved.get("gateway_processing")
    assert (await logs.stats())["calls"] == 1
    assert (await logs.stats())["success"] == 1


async def test_cache_limit_warning_is_logged_as_added_content_only(db_app, monkeypatch):
    row, _ctx, raw, call = await fixture_call(db_app, monkeypatch)
    monkeypatch.setattr(db_app.app.state.gateway.embedded, "max_item_bytes", 1)
    result = await call()
    assert not result.is_error and not db_app.app.state.gateway.embedded.items
    identity = (await db_app.app.state.logs.query(mcp_id=row.id))["items"][0]["id"]
    saved = await db_app.app.state.logs.detail(identity)
    assert saved["result"] == raw
    added = saved["gateway_processing"]["added_content"]
    assert len(added) == 1 and added[0]["text"] == result.content[-1].text
    assert "exceed" in added[0]["text"] and "UPSTREAM_BODY_MARKER" not in json.dumps(added)


async def test_postprocessing_permission_failure_preserves_downstream_log_outcome(db_app, monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()
    async def delayed(token_id):
        entered.set()
        await finish.wait()
    row, ctx, raw, call = await fixture_call(db_app, monkeypatch, during_call=delayed)
    _user, token, _ = await db_app.app.state.gateway.principal(ctx.request)
    task = asyncio.create_task(call())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with db_app.db.locked() as session:
            (await session.get(ApiToken, token.id)).disabled = True
    finally:
        finish.set()
    result = await asyncio.wait_for(task, 5)
    assert result.is_error
    page = await db_app.app.state.logs.query(mcp_id=row.id)
    assert page["total"] == 1
    saved = await db_app.app.state.logs.detail(page["items"][0]["id"])
    assert saved["status"] == "success" and saved["result"] == raw
    assert "gateway_processing" not in saved


async def test_terminal_log_failure_keeps_processed_client_response(db_app, monkeypatch):
    _row, _ctx, _raw, call = await fixture_call(db_app, monkeypatch)
    logs = db_app.app.state.logs
    append = logs.append
    async def failing_append(event):
        if event["status"] != "running":
            raise OSError("synthetic final log failure")
        return await append(event)
    monkeypatch.setattr(logs, "append", failing_append)
    result = await call()
    assert not result.is_error
    assert next(iter(db_app.app.state.gateway.embedded.items)) in result.content[-1].text
