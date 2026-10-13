"""Capability discovery must enforce budgets before aggregating and publishing."""
import asyncio
from typing import ClassVar

import httpx
import pytest

from mcp_manager import transports
from mcp_manager.runtime import GatewayError


def budget(**values):
    assert hasattr(transports, "DiscoveryBudget"), "Discovery must bound pages, entries and decoded bytes"
    return transports.DiscoveryBudget(**values)


class PagedClient:
    server_capabilities: ClassVar[dict] = {"tools": {}}
    server_info: ClassVar[dict] = {"name": "fixture", "version": "1"}
    protocol_version = "2025-03-26"

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    async def list_tools(self, *, cursor, cache_mode):
        assert cache_mode == "bypass"
        index = int(cursor) if cursor else 0
        self.calls += 1
        return self.pages[index]


async def test_page_limit_stops_before_fetching_an_extra_page():
    client = PagedClient([
        {"tools": [{"name": "a"}], "nextCursor": "1"},
        {"tools": [{"name": "b"}], "nextCursor": "2"},
        {"tools": [{"name": "c"}]},
    ])
    connection = transports.SdkConnection(client)
    with pytest.raises(GatewayError) as caught:
        async for _ in transports.iter_capabilities(connection, budget=budget(max_pages=2)):
            pass
    assert caught.value.code == "discovery_budget_exceeded"
    assert client.calls == 2


async def test_entries_and_bytes_are_checked_before_publication():
    client = PagedClient([{"tools": [{"name": "a"}, {"name": "b"}]}])
    with pytest.raises(GatewayError):
        async for _ in transports.iter_capabilities(transports.SdkConnection(client), budget=budget(max_entries=1)):
            pass
    client = PagedClient([{"tools": [{"name": "a", "description": "中" * 100}]}])
    with pytest.raises(GatewayError) as caught:
        async for _ in transports.iter_capabilities(transports.SdkConnection(client), budget=budget(max_message_bytes=100)):
            pass
    assert caught.value.code == "discovery_budget_exceeded"


async def test_iteration_never_yields_more_than_a_staging_batch():
    client = PagedClient([{"tools": [{"name": str(index)} for index in range(501)]}])
    pages = [page async for page in transports.iter_capabilities(transports.SdkConnection(client), budget=budget())]
    tools = [items for kind, items in pages if kind == "tools"]
    assert [len(items) for items in tools] == [250, 250, 1]


def test_no_newline_message_is_bounded_and_utf8_split_is_preserved():
    from mcp_manager import posix_transport
    assert hasattr(posix_transport, "BoundedMessageBuffer"), "Unterminated stdio messages must not grow forever"
    buffer = posix_transport.BoundedMessageBuffer(max_bytes=10)
    with pytest.raises(GatewayError):
        buffer.feed(b"x" * 11)
    buffer = posix_transport.BoundedMessageBuffer(max_bytes=100)
    message = '{"name":"工具😀"}\n'.encode()
    result = []
    for byte in message:
        result.extend(buffer.feed(bytes([byte])))
    assert result == ['{"name":"工具😀"}']


async def test_rest_probe_concurrency_and_fixed_healthcheck():
    active, peak, seen = 0, 0, []
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen.append(request.method)
        if active == 4:
            entered.set()
        await release.wait()
        active -= 1
        return httpx.Response(200)
    config = {"tools": [{"name": str(index), "request": {
        "method": "POST", "url": f"https://example.invalid/route/{index}"
    }} for index in range(20)]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connection = transports.RestConnection(config, client)
        task = asyncio.create_task(connection.discover())
        try:
            await asyncio.wait_for(entered.wait(), 1)
        finally:
            release.set()
        result = await asyncio.wait_for(task, 5)
    assert peak == 4 and len(result["tools"]) == 20
    assert seen == ["HEAD"] * 20


async def test_cancelled_index_refresh_is_not_published(dual_db, db_settings):
    from mcp_manager.artifact_store import ArtifactStore
    from mcp_manager.catalog_store import CatalogStore
    from mcp_manager.catalog_types import CatalogScope
    from mcp_manager.database import McpServer

    assert hasattr(transports, "ingest_capabilities"), "Discovery needs staged ingestion before publish"
    async with dual_db.locked() as session:
        row = McpServer(name="fixture", slug="fixture", transport="stdio", config={})
        session.add(row)
        await session.flush()
    scope = CatalogScope(row.id, "service", row.revision, "v1")
    store = CatalogStore(dual_db, ArtifactStore(db_settings.data_dir), signing_key="synthetic")
    entered = asyncio.Event()
    class WaitingClient(PagedClient):
        async def list_tools(self, *, cursor, cache_mode):
            if cursor:
                entered.set()
                await asyncio.Event().wait()
            return {"tools": [{"name": "partial", "inputSchema": {"type": "object"}}], "nextCursor": "1"}
    task = asyncio.create_task(transports.ingest_capabilities(
        transports.SdkConnection(WaitingClient([])), store, scope, budget=budget()
    ))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert (await store.get_summary(scope))["status"] != "ready"


async def test_mid_capability_failure_discards_its_partial_entries(dual_db, db_settings):
    from mcp_manager.artifact_store import ArtifactStore
    from mcp_manager.catalog_store import CatalogStore
    from mcp_manager.catalog_types import CatalogScope
    from mcp_manager.database import McpServer

    assert hasattr(transports, "ingest_capabilities")
    async with dual_db.locked() as session:
        row = McpServer(name="fixture", slug="fixture", transport="stdio", config={})
        session.add(row)
        await session.flush()
    scope = CatalogScope(row.id, "service", row.revision, "v1")
    store = CatalogStore(dual_db, ArtifactStore(db_settings.data_dir), signing_key="synthetic")
    class PartialClient(PagedClient):
        server_capabilities: ClassVar[dict] = {"tools": {}, "prompts": {}}
        async def list_prompts(self, *, cursor, cache_mode):
            if cursor:
                raise GatewayError("method_error", "Synthetic prompt page failure")
            return {"prompts": [{"name": "partial-prompt"}], "nextCursor": "1"}
    result = await transports.ingest_capabilities(
        transports.SdkConnection(PartialClient([{"tools": [{"name": "complete", "inputSchema": {"type": "object"}}]}])),
        store, scope, budget=budget(),
    )
    assert result["tool_count"] == 1 and result["prompt_count"] == 0
    assert result["capability_errors"][0]["capability"] == "prompts"
    assert (await store.get_summary(scope)).get("capability_errors") == result["capability_errors"]


async def test_catalog_refresh_uses_streamed_index_and_returns_only_summary(db_app, monkeypatch):
    import json
    from contextlib import asynccontextmanager

    from mcp_manager.database import McpServer

    catalog = db_app.app.state.catalog
    assert hasattr(catalog, "refresh_index"), "Catalog refresh needs an indexed streaming entry point"
    row = await catalog.create({
        "name": "Large indexed service", "transport": "stdio", "mode": "disabled",
        "config": {"command": "synthetic-never-launched"},
    })
    async with db_app.db.locked() as session:
        (await session.get(McpServer, row.id)).mode = "lazy"
    tools = [{"name": f"tool-{index:04}", "inputSchema": {
        "type": "object", "properties": {"path": {"type": "string"}}
    }} for index in range(501)]
    @asynccontextmanager
    async def connector(spec):
        yield transports.SdkConnection(PagedClient([{"tools": tools}]), spec.config)
    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    result = await catalog.refresh_index(row.id)
    assert result["tool_count"] == 501 and result["status"] == "ready"
    assert len(json.dumps(result).encode()) <= 16384 and "tools" not in result
    scope = await catalog.index_scope(await catalog.get(row.id))
    page = await catalog.index.list_capabilities(scope, kind="tools", query="", cursor=None, limit=50)
    assert page.total == 501 and len(page.items) == 50 and "inputSchema" not in page.items[0]


async def test_credential_change_during_index_refresh_cannot_publish(db_app, monkeypatch):
    from contextlib import asynccontextmanager

    from mcp_manager.database import McpServer

    catalog = db_app.app.state.catalog
    assert hasattr(catalog, "refresh_index")
    row = await catalog.create({
        "name": "Changed credentials", "transport": "stdio", "mode": "disabled",
        "config": {"command": "synthetic-never-launched", "auth": {"type": "bearer", "token": "synthetic-a"}},
    })
    async with db_app.db.locked() as session:
        (await session.get(McpServer, row.id)).mode = "lazy"
    entered, release = asyncio.Event(), asyncio.Event()
    class WaitingClient(PagedClient):
        async def list_tools(self, **kwargs):
            entered.set()
            await release.wait()
            return {"tools": [{"name": "old", "inputSchema": {"type": "object"}}]}
    @asynccontextmanager
    async def connector(spec):
        yield transports.SdkConnection(WaitingClient([]), spec.config)
    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    task = asyncio.create_task(catalog.refresh_index(row.id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with db_app.db.locked() as session:
            await db_app.app.state.credentials.write(session, row.id, "service", {
                "auth_config": {"type": "bearer", "token": "synthetic-b"}
            })
    finally:
        release.set()
    with pytest.raises(GatewayError):
        await asyncio.wait_for(task, 10)
    scope = await catalog.index_scope(await catalog.get(row.id))
    assert (await catalog.index.get_summary(scope))["status"] != "ready"


def test_trickled_message_memory_is_proportional_to_its_byte_budget():
    import tracemalloc

    from mcp_manager.posix_transport import BoundedMessageBuffer

    buffer = BoundedMessageBuffer(max_bytes=100001)
    tracemalloc.start()
    try:
        baseline, _ = tracemalloc.get_traced_memory()
        for _ in range(100000):
            buffer.feed(b"x")
        retained, _ = tracemalloc.get_traced_memory()
        assert retained - baseline < 4 * 100000 + 65536
        assert buffer.feed(b"\n") == ["x" * 100000]
    finally:
        tracemalloc.stop()
