"""Only MCP availability failures consume the owner-scoped health budget."""
import time
from contextlib import asynccontextmanager

import pytest
from test_exact_route_scalability import gateway_context, invoke

from mcp_manager.catalog import alias
from mcp_manager.runtime import GatewayError


async def healthy_service(h, kind="oauth", isolation="service"):
    catalog = h.app.state.catalog
    faults = {"connect": False, "discovery": False, "call": False, "calls": 0, "disconnect": False}
    transport = {"stdio": "stdio", "rest": "rest"}.get(kind, "streamable-http")
    config = {"command": "fixture"} if kind == "stdio" else {"url": "https://mcp.test/mcp"}
    if kind == "rest":
        config["tools"] = [{"name": "echo", "request": {"url": "https://mcp.test/data"}}]
    if kind in {"oauth", "bearer"}:
        config["auth"] = ({"type": "bearer", "token": "fixture"} if kind == "bearer" else {
            "type": "oauth", "auth_mode": "pre_registration", "client_id": "fixture",
            "authorization_url": "https://auth.test/a", "token_url": "https://auth.test/t"})
    row = await catalog.create({
        "name": "Failure budget", "transport": transport, "isolation": isolation, "config": config,
    }, user_id=h.actor["id"], discover=False)
    if kind == "oauth":
        async with h.db.locked() as session:
            await h.app.state.credentials.save_oauth_token(session, row, h.actor["id"], {
                "access_token": "access", "refresh_token": "refresh", "expires_at": time.time() + 3600,
            })

    class Connection:
        async def discover(self):
            if faults["discovery"]:
                raise GatewayError("discovery_failed", "fixture tools/list unavailable")
            return {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}],
                    "resources": [{"uri": "fixture://note", "name": "note"}],
                    "prompts": [{"name": "hint"}], "templates": [{"uriTemplate": "fixture://{name}", "name": "template"}]}

        async def call(self, name, arguments):
            faults["calls"] += 1
            if faults.get("validation"):
                from mcp.types import CallToolResult
                CallToolResult.model_validate({"content": [{"type": "text"}]})
            if faults["disconnect"]:
                import anyio
                raise anyio.EndOfStream
            return {"isError": faults["call"], "content": [{
                "type": "text", "text": "fixture business error" if faults["call"] else "ok"}]}

    @asynccontextmanager
    async def connector(spec):
        if faults["connect"]:
            raise GatewayError("connection_error", "fixture connection unavailable")
        yield Connection()

    h.app.state.runtime.connector = connector
    await catalog.refresh_index(row.id, h.actor["id"])
    context, _ = await gateway_context(h)
    return row, faults, context


async def call(h, row, context, *, fail=False):
    result = await invoke(h, context, "gateway_call", {"name": alias(row.slug, "echo"), "arguments": {}})
    assert result.is_error is fail, result


async def refresh(h, row, *, scheduled=False, fail=False):
    if scheduled:
        operation = h.app.state.catalog.refresh_target(
            {"server_id": row.id, "user_id": h.actor["id"] if row.isolation == "user" else None})
        if fail:
            with pytest.raises(GatewayError):
                await operation
        else:
            await operation
    else:
        response = await h.web.post(f"/api/v1/mcps/{row.id}/refresh")
        assert (response.status_code >= 400) is fail, response.text


async def assert_state(h, row, count, *, retained=True):
    cache = h.app.state.catalog.cached(row, h.actor["id"])
    assert cache["startup_failure_count"] == count
    assert cache["runtime_status"] == ("stopped" if retained else "failed")
    assert bool(cache["cache_at"]) is retained
    response = await h.web.get("/api/v1/mcps")
    item = next(item for item in response.json()["items"] if item["id"] == row.id)
    assert item["tool_count"] == int(retained), item
    if not retained:
        assert item["status"] == "failed", item
    for kind in ("tools", "resources", "prompts", "templates"):
        page = await h.web.get(f"/api/v1/mcps/{row.id}/capabilities", params={"kind": kind})
        assert page.status_code == 200, page.text
        assert page.json()["total"] == int(retained), (kind, page.text)


@pytest.mark.parametrize("isolation", ["service", "user"])
@pytest.mark.parametrize("kind", ["stdio", "rest", "http", "bearer", "oauth"])
async def test_mixed_call_and_refresh_results_share_one_failure_budget(db_app, kind, isolation):
    row, faults, context = await healthy_service(db_app, kind, isolation)
    original_time = db_app.app.state.catalog.cached(row, db_app.actor["id"])["cache_at"]
    faults["discovery"] = True
    await refresh(db_app, row, scheduled=True, fail=True)
    await assert_state(db_app, row, 1)
    await refresh(db_app, row, fail=True)
    await assert_state(db_app, row, 2)
    assert db_app.app.state.catalog.cached(row, db_app.actor["id"])["cache_at"] == original_time
    # A valid response proves the MCP is alive, even when the tool rejects the work.
    faults["call"] = True
    await call(db_app, row, context, fail=True)
    await assert_state(db_app, row, 0)
    await call(db_app, row, context, fail=True)
    await assert_state(db_app, row, 0)
    faults["discovery"] = False
    await refresh(db_app, row)
    faults["disconnect"] = True
    for expected in (1, 2, 3):
        await call(db_app, row, context, fail=True)
        await assert_state(db_app, row, expected, retained=expected < 3)
    assert faults["calls"] == 5

@pytest.mark.parametrize("fault", ["connect", "discovery"])
async def test_connecting_alone_does_not_reset_counter_or_double_count(db_app, fault):
    row, faults, _ = await healthy_service(db_app)
    faults[fault] = True
    for expected in (1, 2, 3):
        await refresh(db_app, row, scheduled=True, fail=True)
        await assert_state(db_app, row, expected, retained=expected < 3)


async def test_configured_threshold_counts_disconnects_not_business_errors(db_app):
    row, faults, context = await healthy_service(db_app, "http")
    response = await db_app.web.patch("/api/v1/settings", json={"startup_failure_threshold": 2})
    assert response.status_code == 200
    faults["call"] = True
    for _ in range(3):
        await call(db_app, row, context, fail=True)
        await assert_state(db_app, row, 0)
    faults["disconnect"] = True
    await call(db_app, row, context, fail=True)
    await assert_state(db_app, row, 1)
    await call(db_app, row, context, fail=True)
    await assert_state(db_app, row, 2, retained=False)

@pytest.mark.parametrize("isolation", ["service", "user"])
async def test_oauth_reauth_blocks_execution_without_counting_mcp_unavailability(db_app, monkeypatch, isolation):
    from mcp_manager import oauth as oauth_module

    row, _faults, context = await healthy_service(db_app, isolation=isolation)
    exchanges = 0

    async def failed_exchange(auth, data):
        nonlocal exchanges
        exchanges += 1
        raise GatewayError("oauth_failed", "fixture refresh rejected")

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", failed_exchange)
    monkeypatch.setattr(oauth_module, "token_expiry", lambda token: 0)
    await call(db_app, row, context, fail=True)
    await assert_state(db_app, row, 0)
    await refresh(db_app, row, scheduled=True, fail=True)
    await assert_state(db_app, row, 0)
    await call(db_app, row, context, fail=True)
    await assert_state(db_app, row, 0)
    assert exchanges == 1


@pytest.mark.parametrize("isolation", ["service", "user"])
async def test_successful_token_renewal_does_not_destroy_last_good_directory(db_app, monkeypatch, isolation):
    row, faults, _context = await healthy_service(db_app, isolation=isolation)
    catalog = db_app.app.state.catalog
    original_time = catalog.cached(row, db_app.actor["id"])["cache_at"]
    oauth = db_app.app.state.oauth
    original_credentials = oauth.credentials
    renewed = False

    async def exchange(auth, data):
        return {"access_token": "renewed", "refresh_token": "rotated", "expires_at": time.time() + 3600}

    async def credentials(target, user, **options):
        nonlocal renewed
        if not renewed:
            renewed = True
            options["force_refresh"] = True
        return await original_credentials(target, user, **options)

    monkeypatch.setattr(oauth, "exchange", exchange)
    monkeypatch.setattr(oauth, "credentials", credentials)
    faults["discovery"] = True
    await refresh(db_app, row, scheduled=True, fail=True)
    await assert_state(db_app, row, 1)
    assert catalog.cached(row, db_app.actor["id"])["cache_at"] == original_time


async def test_internal_scope_retry_counts_one_final_refresh_failure(db_app, monkeypatch):
    row, faults, _context = await healthy_service(db_app)
    catalog = db_app.app.state.catalog
    original = catalog.index.begin_refresh
    attempts = 0

    async def conflict_once(scope):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise GatewayError("credential_changed", "fixture preparation conflict")
        return await original(scope)

    monkeypatch.setattr(catalog.index, "begin_refresh", conflict_once)
    faults["discovery"] = True
    await refresh(db_app, row, scheduled=True, fail=True)
    await assert_state(db_app, row, 1)
    assert attempts == 2


@pytest.mark.parametrize("isolation", ["service", "user"])
async def test_other_user_success_resets_only_the_correct_owner(db_app, isolation):
    from test_oauth_recovery import actor

    from mcp_manager.database import User

    row, faults, context = await healthy_service(db_app, "http", isolation)
    async with actor(db_app, "other-budget-owner") as (_web, identity):
        await db_app.app.state.catalog.refresh_index(row.id, identity)
        faults["disconnect"] = True
        await call(db_app, row, context, fail=True)
        await call(db_app, row, context, fail=True)
        assert db_app.app.state.catalog.cached(row, db_app.actor["id"])["startup_failure_count"] == 2
        faults["disconnect"] = False
        async with db_app.db.session() as session:
            user = await session.get(User, identity)
        lease = db_app.app.state.runtime.create_lease(identity, "test")
        try:
            result = await db_app.app.state.catalog.call(row, lease, "echo", {}, user=user)
            assert not result["isError"]
        finally:
            await db_app.app.state.runtime.release(lease.id)
        mine = db_app.app.state.catalog.cached(row, db_app.actor["id"])
        assert mine["startup_failure_count"] == (0 if isolation == "service" else 2)
        assert db_app.app.state.catalog.cached(row, identity)["startup_failure_count"] == 0


async def test_warning_and_counter_survive_restart(tmp_path):
    from types import SimpleNamespace

    from test_bug1_catalog import console

    async with console(tmp_path) as (app, web, actor):
        harness = SimpleNamespace(app=app, web=web, actor=actor, db=app.state.db)
        row, faults, context = await healthy_service(harness)
        faults["disconnect"] = True
        await call(harness, row, context, fail=True)
        await call(harness, row, context, fail=True)
        connector = app.state.runtime.connector
    async with console(tmp_path) as (app, web, actor):
        harness = SimpleNamespace(app=app, web=web, actor=actor, db=app.state.db)
        row = await app.state.catalog.get(row.id)
        await assert_state(harness, row, 2)
        app.state.runtime.connector = connector
        faults["disconnect"] = False
        context, _ = await gateway_context(harness)
        await call(harness, row, context)
        await assert_state(harness, row, 0)


async def test_concurrent_failures_are_not_lost(db_app):
    import asyncio

    row, _faults, _context = await healthy_service(db_app)
    catalog = db_app.app.state.catalog
    await asyncio.gather(*[
        catalog.record_start_failure(row, GatewayError("connection_error", "fixture parallel failure"),
                                     db_app.actor["id"])
        for _ in range(3)
    ])
    await assert_state(db_app, row, 3, retained=False)


async def test_failed_resource_listing_does_not_publish_a_partial_empty_kind(db_app, monkeypatch):
    row, _faults, _context = await healthy_service(db_app, "http")

    class Partial:
        async def discover(self):
            return {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}],
                    "resources": [], "prompts": [{"name": "hint"}],
                    "templates": [{"uriTemplate": "fixture://{name}", "name": "template"}],
                    "capability_errors": [{"capability": "resources", "error": "fixture resource list unavailable"}]}

    @asynccontextmanager
    async def connector(spec):
        yield Partial()

    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    await refresh(db_app, row, scheduled=True, fail=True)
    await assert_state(db_app, row, 1)


@pytest.mark.parametrize("kind", ["resource", "prompt"])
async def test_resource_and_prompt_calls_share_the_same_budget(db_app, monkeypatch, kind):
    from mcp import types

    row, faults, context = await healthy_service(db_app, "http")
    original = db_app.app.state.runtime.connector

    @asynccontextmanager
    async def connector(spec):
        async with original(spec) as peer:
            async def read_resource(uri):
                if faults["call"]:
                    raise GatewayError("downstream_error", "fixture resource read failed")
                return {"contents": [{"uri": uri, "text": "body"}]}

            async def get_prompt(name, arguments):
                if faults["call"]:
                    raise GatewayError("downstream_error", "fixture prompt failed")
                return {"messages": [{"role": "user", "content": {"type": "text", "text": "hint"}}]}

            peer.read_resource = read_resource
            peer.get_prompt = get_prompt
            yield peer

    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    gateway = db_app.app.state.gateway

    async def read():
        if kind == "prompt":
            return await gateway.get_prompt(context, types.GetPromptRequestParams(name=alias(row.slug, "hint")))
        user, token, _allowed = await gateway.principal(context.request)
        return await gateway.resource_value(
            context.request, user, token, [row], f"mcp-manager://{row.id}/fixture://note")

    faults["call"] = True
    await call(db_app, row, context, fail=True)
    await assert_state(db_app, row, 0)
    faults["call"] = False
    await read()
    await assert_state(db_app, row, 0)
    faults["call"] = True
    with pytest.raises(GatewayError):
        await read()
    await assert_state(db_app, row, 0)


async def test_stdio_session_instances_share_the_service_catalog_budget(db_app):
    row, faults, first = await healthy_service(db_app, "stdio", "session")
    second, _token = await gateway_context(db_app)
    faults["disconnect"] = True
    await call(db_app, row, first, fail=True)
    await call(db_app, row, second, fail=True)
    await assert_state(db_app, row, 2)
    faults["disconnect"] = False
    await call(db_app, row, second)
    await assert_state(db_app, row, 0)


async def test_health_updates_do_not_invalidate_resource_pagination(db_app):
    row, faults, context = await healthy_service(db_app, "http")
    from mcp_manager.database import ApiToken

    _user, token, _allowed = await db_app.app.state.gateway.principal(context.request)
    async with db_app.db.locked() as session:
        (await session.get(ApiToken, token.id)).enable_resource_tools = True
    query = {"mcp": row.id, "kind": "all", "limit": 1}
    first = await invoke(db_app, context, "gateway_search_resources", query)
    assert not first.is_error, first
    cursor = first.structured_content["next_cursor"]
    assert cursor
    faults["call"] = True
    await call(db_app, row, context, fail=True)
    faults["discovery"] = True
    await refresh(db_app, row, scheduled=True, fail=True)
    second = await invoke(db_app, context, "gateway_search_resources", query | {"cursor": cursor})
    assert not second.is_error, second


@pytest.mark.parametrize("isolation", ["service", "user"])
async def test_real_scheduled_jobs_share_manual_and_call_failure_budget(db_app, isolation):
    import asyncio
    from datetime import timedelta

    from mcp_manager import app as app_module
    from mcp_manager.database import set_setting

    row, faults, context = await healthy_service(db_app, isolation=isolation)
    state = db_app.app.state
    await set_setting(db_app.db, "refresh_enabled", True)

    async def scheduled_failure(expected):
        state.refresh_next = app_module.now() - timedelta(seconds=1)
        await app_module._scheduled_refresh(db_app.app)
        identity, task = next(iter(state.jobs.tasks.items()))
        await asyncio.wait_for(task, 15)
        job = await state.jobs.store.get_status(identity)
        assert job["total"] == job["failed"] == 1 and job["succeeded"] == 0
        results = await state.jobs.store.list_results(identity)
        assert not results["items"][0]["ok"]
        await assert_state(db_app, row, expected, retained=expected < 3)

    faults["discovery"] = True
    await scheduled_failure(1)
    await refresh(db_app, row, fail=True)
    await assert_state(db_app, row, 2)
    await call(db_app, row, context)
    await assert_state(db_app, row, 0)
    await scheduled_failure(1)
    await scheduled_failure(2)
    await scheduled_failure(3)
