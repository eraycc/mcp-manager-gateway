"""Web catalog GETs must use indexed summaries and never start a lazy process."""
from sqlalchemy import select

from mcp_manager.database import ApiToken, McpServer, User
from mcp_manager.proposals import submit_proposals


async def indexed_catalog(harness, count=301):
    catalog = harness.app.state.catalog
    row = await catalog.create({
        "name": "Large catalog", "mode": "disabled", "transport": "stdio",
        "config": {"command": "synthetic-never-launched"},
    })
    async with harness.db.locked() as session:
        (await session.get(McpServer, row.id)).mode = "lazy"
    row = await catalog.get(row.id)
    scope = await catalog.index_scope(row)
    generation = await catalog.index.begin_refresh(scope)
    tools = [{"name": f"match-{index:04}", "description": "Useful description", "inputSchema": {
        "type": "object", "properties": {f"field-{field}": {"type": "string"} for field in range(20)}
    }} for index in range(count)]
    for start in range(0, count, 250):
        await catalog.index.stage_page(generation.id, "tools", tools[start:start + 250])
    await catalog.index.publish(generation.id, expected_scope=scope)
    return row, tools


async def test_first_page_is_bounded_schema_free_and_get_never_starts_lazy(db_app, monkeypatch):
    row, _tools = await indexed_catalog(db_app)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Summary GET must not load Schema or start a downstream")
    monkeypatch.setattr(db_app.app.state.catalog.artifacts, "read_json", forbidden)
    monkeypatch.setattr(db_app.app.state.runtime, "perform", forbidden)
    response = await db_app.web.get(f"/api/v1/mcps/{row.id}/capabilities", params={"kind": "tools", "limit": 50})
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 301 and len(page["items"]) == 50 and page["next_cursor"]
    assert len(response.content) <= 131072
    assert all("inputSchema" not in item for item in page["items"])
    next_page = await db_app.web.get(f"/api/v1/mcps/{row.id}/capabilities", params={
        "kind": "tools", "limit": 50, "cursor": page["next_cursor"]
    })
    assert next_page.status_code == 200
    assert next_page.json()["items"][0]["name"] == "match-0050"


async def test_single_definition_is_complete_and_scope_is_checked(db_app, monkeypatch):
    row, tools = await indexed_catalog(db_app, 2)
    result = await db_app.web.get(f"/api/v1/mcps/{row.id}/capabilities/tools/match-0000")
    assert result.status_code == 200 and result.json()["inputSchema"] == tools[0]["inputSchema"]
    await db_app.web.post("/api/v1/auth/register", json={"username": "ordinary", "password": "password12345"})
    await db_app.web.post("/api/v1/auth/login", json={"username": "ordinary", "password": "password12345"})
    denied = await db_app.web.get(f"/api/v1/mcps/{row.id}/capabilities/tools/match-0000")
    assert denied.status_code == 404


async def test_proposal_listing_filters_before_paging_without_unsealing_all_rows(db_app, monkeypatch):
    token_response = await db_app.web.post("/api/v1/tokens", json={"name": "propose", "enable_mcp_proposal": True})
    async with db_app.db.session() as session:
        actor = await session.scalar(select(User).where(User.username == "admin"))
        token = await session.get(ApiToken, token_response.json()["id"])
    proposals = [{"name": f"ordinary-{index}", "transport": "stdio", "config": {"command": "synthetic"}}
                 for index in range(100)]
    await submit_proposals(db_app.app, actor, token, {"proposals": proposals})
    await submit_proposals(db_app.app, actor, token, {"proposals": [
        {"name": f"needle-{index}", "transport": "stdio", "config": {"command": "synthetic"}} for index in range(3)
    ]})
    original, reads = db_app.app.state.catalog.unseal, 0
    def unseal(value):
        nonlocal reads
        reads += 1
        return original(value)
    monkeypatch.setattr(db_app.app.state.catalog, "unseal", unseal)
    response = await db_app.web.get("/api/v1/mcp-proposals", params={"q": "needle", "page_size": 2})
    assert response.status_code == 200 and response.json()["total"] == 3
    assert len(response.json()["items"]) == 2
    assert reads <= 2


async def test_summary_test_response_excludes_large_tool_json(db_app, monkeypatch):
    from contextlib import asynccontextmanager

    from test_discovery_ingestion import PagedClient
    from test_proposal_atomicity import propose

    from mcp_manager.transports import SdkConnection

    identity = await propose(db_app)
    tools = [{"name": f"tool-{index}", "inputSchema": {"type": "object", "description": "x" * 2000}}
             for index in range(1000)]
    @asynccontextmanager
    async def connector(spec):
        yield SdkConnection(PagedClient([{"tools": tools}]), spec.config)
    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    response = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test", params={"summary": True})
    assert response.status_code == 200
    assert len(response.content) <= 16384 and "capabilities" not in response.json()
    assert response.json()["tool_count"] == 1000


async def test_large_summary_pages_obey_byte_budget_without_skipping_names(db_app):
    catalog = db_app.app.state.catalog
    row, _tools = await indexed_catalog(db_app, 0)
    scope = await catalog.index_scope(row)
    generation = await catalog.index.begin_refresh(scope)
    await catalog.index.stage_page(generation.id, "tools", [
        {"name": f"wide-{index:03}", "description": "中" * 1024, "inputSchema": {"type": "object"}}
        for index in range(60)
    ])
    await catalog.index.publish(generation.id, expected_scope=scope)
    seen, cursor = [], None
    while True:
        response = await db_app.web.get(f"/api/v1/mcps/{row.id}/capabilities", params={
            "kind": "tools", "limit": 50, **({"cursor": cursor} if cursor else {})
        })
        assert response.status_code == 200 and len(response.content) <= 131072
        page = response.json()
        seen.extend(item["name"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [f"wide-{index:03}" for index in range(60)]


async def test_test_snapshot_is_paged_and_invalidated_by_edit(db_app, monkeypatch):
    from contextlib import asynccontextmanager

    from test_discovery_ingestion import PagedClient
    from test_proposal_atomicity import propose

    from mcp_manager.transports import SdkConnection

    identity = await propose(db_app)
    @asynccontextmanager
    async def connector(spec):
        yield SdkConnection(PagedClient([{"tools": [
            {"name": f"tool-{index:03}", "inputSchema": {"type": "object"}} for index in range(61)
        ]}]), spec.config)
    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    response = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test", params={"summary": True})
    assert response.status_code == 200
    page = await db_app.web.get(f"/api/v1/mcp-proposals/{identity}/test-capabilities")
    assert page.status_code == 200 and page.json()["total"] == 61 and page.json()["next_cursor"]
    assert all("inputSchema" not in item for item in page.json()["items"])
    definition = await db_app.web.get(f"/api/v1/mcp-proposals/{identity}/test-capabilities/tools/tool-000")
    assert definition.status_code == 200 and definition.json()["inputSchema"] == {"type": "object"}
    await db_app.web.patch(f"/api/v1/mcp-proposals/{identity}", json={"name": "Changed"})
    expired = await db_app.web.get(f"/api/v1/mcp-proposals/{identity}/test-capabilities")
    assert expired.status_code == 404


async def test_service_list_only_assembles_the_sql_page(db_app, monkeypatch):
    catalog = db_app.app.state.catalog
    async with db_app.db.locked() as session:
        for index in range(100):
            await catalog.create_in_session(session, {
                "name": f"service-{index:03}", "transport": "stdio", "mode": "disabled",
                "config": {"command": "synthetic"}
            })
    original, assembled = catalog.public, 0
    def public(row, **kwargs):
        nonlocal assembled
        assembled += 1
        return original(row, **kwargs)
    monkeypatch.setattr(catalog, "public", public)
    response = await db_app.web.get("/api/v1/mcps", params={"page_size": 20})
    assert response.status_code == 200 and response.json()["total"] == 100
    assert assembled <= 20


async def test_legacy_tools_get_never_discovers_a_lazy_service(db_app, monkeypatch):
    catalog = db_app.app.state.catalog
    row = await catalog.create({
        "name": "Cold service", "mode": "disabled", "transport": "stdio",
        "config": {"command": "synthetic-never-launched"},
    })
    async with db_app.db.locked() as session:
        (await session.get(McpServer, row.id)).mode = "lazy"
    attempts = 0
    async def discover(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise AssertionError("Read-only legacy GET must never discover")
    monkeypatch.setattr(catalog, "refresh", discover)
    response = await db_app.web.get(f"/api/v1/mcps/{row.id}/tools")
    assert response.status_code == 200 and attempts == 0


async def test_index_scope_does_not_load_a_sealed_full_configuration(db_app):
    import re

    from sqlalchemy import event
    from sqlalchemy.orm import load_only

    catalog = db_app.app.state.catalog
    row, _tools = await indexed_catalog(db_app, 1)
    statements = []
    def trace(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(db_app.db.engine.sync_engine, "before_cursor_execute", trace)
    try:
        async with db_app.db.session() as session:
            projected = await session.get(McpServer, row.id, options=[load_only(
                McpServer.id, McpServer.revision, McpServer.isolation
            )])
            scope = await catalog.index_scope(projected)
            summary = await catalog.index.get_summary(scope)
        assert summary["tool_count"] == 1
        assert not any(re.search(r"mcp_servers\.config(?:\s|,)", sql) for sql in statements)
    finally:
        event.remove(db_app.db.engine.sync_engine, "before_cursor_execute", trace)


async def test_failed_new_generation_cannot_return_an_old_page(db_app, monkeypatch):
    import asyncio

    catalog = db_app.app.state.catalog
    row, _tools = await indexed_catalog(db_app, 2)
    scope = await catalog.index_scope(row)
    entered, release = asyncio.Event(), asyncio.Event()
    original = catalog.index.list_capabilities
    async def delayed(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(catalog.index, "list_capabilities", delayed)
    task = asyncio.create_task(db_app.web.get(f"/api/v1/mcps/{row.id}/capabilities"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        generation = await catalog.index.begin_refresh(scope)
        await catalog.index.fail_refresh(generation.id)
    finally:
        release.set()
    response = await asyncio.wait_for(task, 5)
    assert response.status_code == 502
    assert response.json()["code"] == "revision_changed"


async def test_legacy_tools_budget_is_checked_before_copying_full_schemas(db_app, monkeypatch):
    row, _tools = await indexed_catalog(db_app, 0)
    catalog = db_app.app.state.catalog
    key = catalog.cache_key(row)
    catalog.cache_store.items[key] = {"revision": row.revision, "data": {
        "cache_status": "ready", "tools": [{"name": "huge", "inputSchema": {"description": "中" * 400000}}],
    }}
    copies = 0
    original = catalog.cached
    def copied(*args, **kwargs):
        nonlocal copies
        copies += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(catalog, "cached", copied)
    response = await db_app.web.get(f"/api/v1/mcps/{row.id}/tools")
    assert response.status_code == 502 and response.json()["code"] == "catalog_payload_too_large"
    assert copies == 0


async def test_service_listing_uses_indexed_counts_without_reading_definitions(db_app, monkeypatch):
    row, _tools = await indexed_catalog(db_app, 7)
    async def forbidden(*args, **kwargs):
        raise AssertionError("A service summary must never read definitions")
    monkeypatch.setattr(db_app.app.state.catalog.artifacts, "read_json", forbidden)
    response = await db_app.web.get("/api/v1/mcps")
    assert response.status_code == 200
    item = next(value for value in response.json()["items"] if value["id"] == row.id)
    assert item["tool_count"] == 7 and item["cache_status"] == "ready"


async def test_stale_proposal_configuration_cannot_overwrite_current_revision(db_app):
    from test_proposal_atomicity import propose

    identity = await propose(db_app)
    first = await db_app.web.patch(f"/api/v1/mcp-proposals/{identity}", json={"revision": 1, "name": "Current"})
    assert first.status_code == 200
    stale = await db_app.web.patch(f"/api/v1/mcp-proposals/{identity}", json={"revision": 1, "name": "Stale"})
    assert stale.status_code == 409
    detail = await db_app.web.get(f"/api/v1/mcp-proposals/{identity}")
    assert detail.json()["name"] == "Current"


async def test_oversized_service_metadata_is_an_explicit_bounded_error(db_app):
    catalog = db_app.app.state.catalog
    async with db_app.db.locked() as session:
        for index in range(50):
            await catalog.create_in_session(session, {
                "name": f"wide-{index}", "description": "中" * 2048,
                "transport": "stdio", "mode": "disabled", "config": {"command": "synthetic"},
            })
    response = await db_app.web.get("/api/v1/mcps", params={"page_size": 50})
    assert response.status_code == 502 and response.json()["code"] == "catalog_payload_too_large"
    assert len(response.content) <= 131072


async def test_snapshot_queries_search_descriptions_and_escape_literals(db_app, monkeypatch):
    from contextlib import asynccontextmanager

    from test_discovery_ingestion import PagedClient
    from test_proposal_atomicity import propose

    from mcp_manager.transports import SdkConnection

    identity = await propose(db_app)
    @asynccontextmanager
    async def connector(spec):
        yield SdkConnection(PagedClient([{"tools": [
            {"name": "Alpha", "description": "Needle 中", "inputSchema": {"type": "object"}},
            {"name": r"literal\_%", "inputSchema": {"type": "object"}},
        ]}]), spec.config)
    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    tested = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test", params={"summary": True})
    assert tested.status_code == 200
    for query, expected in [("needle", "Alpha"), (r"\_%", r"literal\_%")]:
        response = await db_app.web.get(f"/api/v1/mcp-proposals/{identity}/test-capabilities", params={"q": query})
        assert response.status_code == 200 and response.json()["total"] == 1
        assert response.json()["items"][0]["name"] == expected


async def test_snapshot_expires_and_is_not_shared_between_administrators(db_app, monkeypatch):
    from contextlib import asynccontextmanager
    from datetime import timedelta

    from test_discovery_ingestion import PagedClient
    from test_proposal_atomicity import propose

    from mcp_manager.database import McpProposal, now
    from mcp_manager.transports import SdkConnection

    identity = await propose(db_app)
    @asynccontextmanager
    async def connector(spec):
        yield SdkConnection(PagedClient([{"tools": [{"name": "private", "inputSchema": {"type": "object"}}]}]), spec.config)
    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    assert (await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test", params={"summary": True})).status_code == 200
    assert (await db_app.web.get(f"/api/v1/mcp-proposals/{identity}/test-capabilities")).status_code == 200
    await db_app.web.post("/api/v1/auth/register", json={"username": "other-admin", "password": "password12345"})
    async with db_app.db.locked() as session:
        other = await session.scalar(select(User).where(User.username == "other-admin"))
        other.role = "admin"
    await db_app.web.post("/api/v1/auth/login", json={"username": "other-admin", "password": "password12345"})
    assert (await db_app.web.get(f"/api/v1/mcp-proposals/{identity}/test-capabilities")).status_code == 404
    await db_app.web.post("/api/v1/auth/login", json={"username": "admin", "password": "password12345"})
    async with db_app.db.locked() as session:
        proposal = await session.get(McpProposal, identity)
        proposal.tested_at = now() - timedelta(hours=25)
    assert (await db_app.web.get(f"/api/v1/mcp-proposals/{identity}/test-capabilities")).status_code == 404


async def test_capability_summary_get_never_selects_full_configuration(db_app):
    import re

    from sqlalchemy import event

    row, _tools = await indexed_catalog(db_app, 1)
    statements = []
    def trace(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(db_app.db.engine.sync_engine, "before_cursor_execute", trace)
    try:
        response = await db_app.web.get(f"/api/v1/mcps/{row.id}/capabilities")
        assert response.status_code == 200
        assert not any(re.search(r"mcp_servers\.config(?:\s|,)", sql) for sql in statements)
    finally:
        event.remove(db_app.db.engine.sync_engine, "before_cursor_execute", trace)
