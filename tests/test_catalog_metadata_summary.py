"""Oversized display metadata must not prevent atomic capability publication."""
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest
from test_catalog_store import catalog_store
from test_proposal_atomicity import propose

from mcp_manager.catalog_store import MAX_METADATA_BYTES, metadata_summary
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.db_types import strict_json_dumps


def metadata(*, large=True):
    return {"serverInfo": {"name": "Remote design service", "version": "1.0", "websiteUrl": "https://mcp.example.com",
        "icons": [{"src": "data:image/svg+xml;base64," + "A" * (6400 if large else 16),
                   "mimeType": "image/svg+xml"} for _ in range(3)]},
        "protocolVersion": "2026-07-28", "capability_errors": [{"capability": "prompts", "error": "Unavailable"}]}


@pytest.mark.parametrize("large", [False, True])
async def test_metadata_summary_preserves_identity_errors_and_input(dual_db, db_settings, large):
    store, _, row = await catalog_store(dual_db, db_settings)
    scope = CatalogScope(row.id, "alice", row.revision, "v1")
    generation = await store.begin_refresh(scope)
    original = metadata(large=large)
    before = deepcopy(original)
    await store.set_metadata(generation.id, original)
    await store.publish(generation.id, expected_scope=scope)
    summary = await store.get_summary(scope)
    assert original == before
    assert summary["serverInfo"]["name"] == original["serverInfo"]["name"]
    assert summary["serverInfo"]["version"] == "1.0"
    assert summary["serverInfo"]["websiteUrl"] == "https://mcp.example.com"
    assert summary["capability_errors"] == original["capability_errors"]
    assert summary["protocolVersion"] == original["protocolVersion"]
    if large:
        assert "icons" not in summary["serverInfo"]
        assert summary["metadata_omitted_fields"] == ["serverInfo.icons"]
    else:
        assert summary["serverInfo"]["icons"] == original["serverInfo"]["icons"]
        assert "metadata_omitted_fields" not in summary


async def test_large_identity_metadata_is_bounded_and_still_publishes(dual_db, db_settings):
    store, _, row = await catalog_store(dual_db, db_settings)
    scope = CatalogScope(row.id, "alice", row.revision, "v1")
    generation = await store.begin_refresh(scope)
    value = metadata(large=False)
    value["serverInfo"]["name"] = "名" * 8192
    await store.set_metadata(generation.id, value)
    await store.publish(generation.id, expected_scope=scope)
    summary = await store.get_summary(scope)
    assert summary["status"] == "ready" and summary["metadata_truncated"]
    assert summary["serverInfo"]["name"].startswith("名")
    assert "serverInfo.name" in summary["metadata_truncated_fields"]
    assert len(strict_json_dumps(summary).encode("utf-8")) < 16384


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("extension", [False, True])
async def test_large_metadata_does_not_hide_tools_or_truncate_schemas(db_app, monkeypatch, streaming, extension):
    data = metadata()
    if extension:
        data["serverInfo"]["_meta"] = {"large_extension": "扩展" * 10000}
    tools = [{"name": f"design_{index:02d}", "description": "Read a design",
              "inputSchema": {"type": "object", "properties": {
                  "node_id": {"type": "string", "description": "节点说明" * 1000}}}} for index in range(45)]

    @asynccontextmanager
    async def connector(spec):
        class Peer:
            async def discover(self):
                return {**data, "tools": tools, "resources": [], "prompts": [], "templates": []}
        peer = Peer()
        if streaming:
            async def pages(*, budget):
                for offset in range(0, len(tools), 20):
                    page = tools[offset:offset + 20]
                    await budget.consume({"tools": page}, len(page))
                    yield "tools", page
                yield "_metadata", [data]
            peer.iter_capabilities = pages
        yield peer

    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    created = await db_app.web.post("/api/v1/mcps", json={
        "name": "Large icon service", "mode": "lazy", "transport": "stdio", "config": {"command": "synthetic"}})
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["cache_status"] == "ready" and row["tool_count"] == 45
    cursor, names = None, []
    while True:
        params = {"kind": "tools", "limit": 20}
        if cursor:
            params["cursor"] = cursor
        page = await db_app.web.get(f'/api/v1/mcps/{row["id"]}/capabilities', params=params)
        assert page.status_code == 200, page.text
        result = page.json()
        assert result["total"] == 45
        names.extend(item["name"] for item in result["items"])
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert names == [tool["name"] for tool in tools]
    server = await db_app.app.state.catalog.get(row["id"])
    scope = await db_app.app.state.catalog.index_scope(server, db_app.actor["id"])
    for tool in (tools[0], tools[-1]):
        exact = await db_app.app.state.catalog.index.get_definition(scope, kind="tools", name=tool["name"])
        assert exact == tool
    summary = await db_app.app.state.catalog.index.get_summary(scope)
    assert "icons" not in summary["serverInfo"]
    assert len(strict_json_dumps(summary).encode("utf-8")) < 16384
    assert "icons" in data["serverInfo"]


async def test_proposal_diagnostic_uses_the_same_bounded_metadata_summary(db_app, monkeypatch):
    data = metadata()
    data["serverInfo"]["description"] = "服务说明" * 10000

    @asynccontextmanager
    async def connector(spec):
        class Peer:
            async def discover(self):
                return {**data, "tools": [{"name": "design", "inputSchema": {"type": "object"}}],
                        "resources": [], "prompts": [], "templates": []}
        yield Peer()

    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    identity = await propose(db_app, config={"command": "synthetic"})
    result = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test?summary=true")
    assert result.status_code == 200, result.text
    summary = result.json()
    assert summary["tool_count"] == 1
    assert "icons" not in summary["serverInfo"]
    assert summary["metadata_truncated"]
    assert "serverInfo.description" in summary["metadata_truncated_fields"]
    assert "serverInfo.icons" in summary["metadata_omitted_fields"]
    page = await db_app.web.get(f"/api/v1/mcp-proposals/{identity}/test-capabilities")
    assert page.status_code == 200 and page.json()["total"] == 1


@pytest.mark.parametrize("case", ["description", "extensions", "long_keys", "errors", "escaping", "no_server_info"])
def test_arbitrary_oversized_metadata_produces_a_bounded_summary(case):
    value = metadata(large=False)
    if case == "description":
        value["serverInfo"]["description"] = "服务😀" * 10000
    elif case == "extensions":
        value["serverInfo"]["_meta"] = {"extension": "x" * 20000}
        value["custom_extension"] = ["x" * 20000]
    elif case == "long_keys":
        value["serverInfo"]["k" * 20000] = "large key"
    elif case == "errors":
        value["capability_errors"] = [
            {"capability": "unknown-" + str(index), "error": "other " * 1000} for index in range(300)]
        value["capability_errors"] += [
            {"capability": kind, "error": "错误😀" * 10000} for kind in ("tools", "resources", "prompts", "templates")]
    elif case == "escaping":
        for key in ("name", "title", "version", "description", "websiteUrl"):
            value["serverInfo"][key] = (chr(0) + chr(92) + "😀") * 4000
        value["protocolVersion"] = chr(0) * 10000
        value["capability_errors"] = [{"capability": "tools", "error": chr(0) * 10000}]
    else:
        value["serverInfo"] = None
        value["extension"] = "x" * 20000
    before = deepcopy(value)
    result = metadata_summary(value)
    assert value == before
    assert result["metadata_truncated"]
    assert len(strict_json_dumps(result).encode("utf-8")) <= MAX_METADATA_BYTES
    assert metadata_summary(result) == result
    assert result["capability_errors"], "Warnings must remain visible when their text is shortened"
    if case == "errors":
        assert {error["capability"] for error in result["capability_errors"]} == {"tools", "resources", "prompts", "templates"}
        assert result["capability_errors_total"] == 304
        assert result["capability_errors_truncated"]


@pytest.mark.parametrize("streaming", [False, True])
async def test_raw_metadata_still_obeys_the_discovery_message_budget(streaming):
    from mcp_manager.runtime import GatewayError
    from mcp_manager.transports import DiscoveryBudget, iter_capabilities

    data = metadata()
    class Peer:
        async def discover(self):
            return data
    peer = Peer()
    if streaming:
        async def pages(*, budget):
            yield "_metadata", [data]
        peer.iter_capabilities = pages
    with pytest.raises(GatewayError) as caught:
        async for _ in iter_capabilities(peer, budget=DiscoveryBudget(max_message_bytes=1024)):
            pass
    assert caught.value.code == "discovery_budget_exceeded"


async def test_streamed_metadata_counts_bytes_once_without_consuming_a_listing_page():
    import json

    from mcp_manager.transports import DiscoveryBudget, iter_capabilities

    data, tools = metadata(large=False), {"tools": []}
    class Peer:
        async def iter_capabilities(self, *, budget):
            await budget.consume(tools, 0)
            yield "tools", []
            yield "_metadata", [data]
    budget = DiscoveryBudget(max_pages=1)
    result = [item async for item in iter_capabilities(Peer(), budget=budget)]
    assert len(result) == 2 and budget.pages == 1
    assert budget.decoded_bytes == sum(len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
                                       for value in (tools, data))


async def test_legacy_metadata_is_not_counted_twice():
    import json

    from mcp_manager.transports import DiscoveryBudget, iter_capabilities

    data = metadata(large=False)
    class Peer:
        async def discover(self):
            return data
    size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    budget = DiscoveryBudget(max_decoded_bytes=size)
    result = [item async for item in iter_capabilities(Peer(), budget=budget)]
    assert result[-1] == ("_metadata", [data])
    assert budget.decoded_bytes == size
