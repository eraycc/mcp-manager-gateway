"""Expired OAuth is renewed before exact-call and catalog scope snapshots."""
import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from test_exact_route_scalability import gateway_context, invoke
from test_oauth_reauth import oauth_row, save_token

from mcp_manager.catalog import alias


async def expired_catalog(harness, monkeypatch, isolation="user"):
    if isolation == "user":
        row, user = await oauth_row(harness)
    else:
        user = harness.actor["id"]
        row = await harness.app.state.catalog.create({
            "name": "Shared OAuth", "transport": "streamable-http", "isolation": "service",
            "config": {"url": "https://mcp.test/mcp", "auth": {
                "type": "oauth", "auth_mode": "pre_registration", "client_id": "public",
                "authorization_url": "https://auth.test/authorize", "token_url": "https://auth.test/token"}},
        }, user_id=user)
    await save_token(harness, row, user, {
        "access_token": "expired", "refresh_token": "refresh", "expires_at": 0,
    })
    catalog = harness.app.state.catalog
    scope = await catalog.index_scope(row, user)
    generation = await catalog.index.begin_refresh(scope)
    tool = {"name": "whoami", "inputSchema": {"type": "object"}}
    await catalog.index.stage_page(generation.id, "tools", [tool])
    await catalog.index.publish(generation.id, expected_scope=scope)
    observed = {"refreshes": 0, "calls": [], "connections": []}

    async def exchange(auth, data):
        assert data == {"grant_type": "refresh_token", "refresh_token": "refresh"}
        observed["refreshes"] += 1
        return {"access_token": "renewed", "expires_at": time.time() + 3600}

    class Connection:
        async def discover(self):
            return {"tools": [tool], "resources": [], "prompts": [], "templates": []}

        async def call(self, name, arguments):
            observed["calls"].append((name, arguments))
            return {"content": [{"type": "text", "text": "ok"}]}

    @asynccontextmanager
    async def connector(spec):
        observed["connections"].append(spec.config["auth"]["access_token"])
        assert spec.config["auth"]["access_token"] == "renewed"
        yield Connection()

    monkeypatch.setattr(harness.app.state.oauth, "exchange", exchange)
    monkeypatch.setattr(harness.app.state.runtime, "connector", connector)
    return row, user, observed


@pytest.mark.parametrize("isolation", ["user", "service"])
@pytest.mark.parametrize("entrypoint", ["gateway", "manual", "scheduled", "warm", "legacy"])
async def test_first_operation_after_token_expiry_succeeds(db_app, monkeypatch, entrypoint, isolation):
    row, user, observed = await expired_catalog(db_app, monkeypatch, isolation)
    catalog = db_app.app.state.catalog
    if entrypoint == "gateway":
        context, _ = await gateway_context(db_app)
        result = await invoke(db_app, context, "gateway_call", {
            "name": alias(row.slug, "whoami"), "arguments": {},
        })
        assert not result.is_error, result
        assert observed["calls"] == [("whoami", {})]
    elif entrypoint == "manual":
        response = await db_app.web.post(f"/api/v1/mcps/{row.id}/refresh")
        assert response.status_code == 200, response.text
    elif entrypoint == "scheduled":
        targets = await catalog.refresh_targets(row.id)
        assert targets == [{"server_id": row.id, "user_id": user if isolation == "user" else None}]
        await catalog.refresh_target(targets[0])
    elif entrypoint == "warm":
        await catalog.warm_target({"server_id": row.id, "user_id": user})
    else:
        await catalog.refresh(row.id, user)
    assert observed["refreshes"] == 1
    assert observed["connections"]
    scope = await catalog.index_scope(row, user)
    summary = await catalog.index.get_summary(scope)
    assert summary["tool_count"] == 1


async def test_oauth_status_renews_expired_token_without_starting_downstream(db_app, monkeypatch):
    row, user, observed = await expired_catalog(db_app, monkeypatch)
    response = await db_app.web.get(f"/api/v1/mcps/{row.id}/oauth/status")
    assert response.status_code == 200, response.text
    assert response.json()["authorized"] is True
    assert response.json()["auth_status"] == "valid"
    assert response.json()["refresh_available"] is True
    assert observed["refreshes"] == 1 and observed["connections"] == []
    assert (await db_app.app.state.credentials.load(row.id, user))["oauth_token"]["access_token"] == "renewed"


@pytest.mark.parametrize("already_valid", [False, True])
async def test_explicit_oauth_refresh_renews_token_without_tool_execution(db_app, monkeypatch, already_valid):
    row, user, observed = await expired_catalog(db_app, monkeypatch)
    if already_valid:
        await save_token(db_app, row, user, {
            "access_token": "still-valid", "refresh_token": "refresh", "expires_at": time.time() + 3600,
        })
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/refresh")
    assert response.status_code == 200, response.text
    assert response.json()["authorized"] is True
    assert observed["refreshes"] == 1
    assert observed["calls"] == [] and observed["connections"] == []


async def test_token_replaced_during_tool_execution_is_not_replayed(db_app, monkeypatch):
    row, user, observed = await expired_catalog(db_app, monkeypatch)
    await db_app.app.state.oauth.credentials(row, user)
    await db_app.app.state.catalog.refresh_index(row.id, user)

    class Connection:
        async def discover(self):
            return {"tools": [{"name": "whoami", "inputSchema": {"type": "object"}}]}

        async def call(self, name, arguments):
            observed["calls"].append((name, arguments))
            await save_token(db_app, row, user, {"access_token": "replacement", "expires_at": None})
            return {"content": [{"type": "text", "text": "must not release stale response"}]}

    @asynccontextmanager
    async def connector(spec):
        yield Connection()

    await db_app.app.state.runtime.stop_server(row.id)
    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    context, _ = await gateway_context(db_app)
    result = await invoke(db_app, context, "gateway_call", {
        "name": alias(row.slug, "whoami"), "arguments": {},
    })
    assert result.is_error and result.structured_content["error"]["code"] == "credential_changed"
    assert observed["calls"] == [("whoami", {})]


async def test_concurrent_first_calls_renew_once_and_each_execute_once(db_app, monkeypatch):
    row, _user, observed = await expired_catalog(db_app, monkeypatch)
    context, _ = await gateway_context(db_app)
    results = await asyncio.gather(*[
        invoke(db_app, context, "gateway_call", {"name": alias(row.slug, "whoami"), "arguments": {}})
        for _ in range(2)
    ])
    assert all(not result.is_error for result in results), results
    assert observed["refreshes"] == 1
    assert observed["calls"] == [("whoami", {}), ("whoami", {})]


@pytest.mark.parametrize("entrypoint", ["gateway", "manual", "scheduled", "personal"])
async def test_scope_rotation_during_preparation_is_retried_in_same_operation(db_app, monkeypatch, entrypoint):
    row, user, observed = await expired_catalog(db_app, monkeypatch)
    oauth = db_app.app.state.oauth
    original = oauth.credentials
    rotated = False

    async def concurrent_rotation(target, actor, **options):
        nonlocal rotated
        result = await original(target, actor, **options)
        if not rotated:
            rotated = True
            # Another session completed a renewal between token acquisition and materialization.
            await save_token(db_app, row, user, result[0] if options.get("with_epoch") else result)
        return result

    monkeypatch.setattr(oauth, "credentials", concurrent_rotation)
    if entrypoint == "gateway":
        context, _ = await gateway_context(db_app)
        response = await invoke(db_app, context, "gateway_call", {
            "name": alias(row.slug, "whoami"), "arguments": {},
        })
        assert not response.is_error, response
        assert observed["calls"] == [("whoami", {})]
    elif entrypoint == "manual":
        response = await db_app.web.post(f"/api/v1/mcps/{row.id}/refresh")
        assert response.status_code == 200, response.text
    elif entrypoint == "personal":
        response = await db_app.web.post(f"/api/v1/mcps/{row.id}/credentials/refresh",
                                         json={"revision": row.revision})
        assert response.status_code == 200, response.text
        assert response.json()["tool_count"] == 1
    else:
        await db_app.app.state.catalog.refresh_target({"server_id": row.id, "user_id": user})
    assert observed["refreshes"] == 1
    summary = await db_app.app.state.catalog.index.get_summary(
        await db_app.app.state.catalog.index_scope(row, user))
    assert summary["tool_count"] == 1


async def test_repeated_scope_rotation_stops_before_business_dispatch(db_app, monkeypatch):
    row, user, observed = await expired_catalog(db_app, monkeypatch)
    oauth = db_app.app.state.oauth
    original, rotations = oauth.credentials, 0

    async def unstable(target, actor, **options):
        nonlocal rotations
        result = await original(target, actor, **options)
        rotations += 1
        await save_token(db_app, row, user, result[0] if options.get("with_epoch") else result)
        return result

    monkeypatch.setattr(oauth, "credentials", unstable)
    context, _ = await gateway_context(db_app)
    result = await invoke(db_app, context, "gateway_call", {
        "name": alias(row.slug, "whoami"), "arguments": {},
    })
    assert result.is_error and result.structured_content["error"]["code"] == "credential_changed"
    assert rotations == 2
    assert observed["calls"] == []


async def test_concurrent_explicit_refreshes_share_one_token_exchange(db_app, monkeypatch):
    row, user, observed = await expired_catalog(db_app, monkeypatch)
    oauth = db_app.app.state.oauth
    both_arrived = asyncio.Event()
    lock = asyncio.Lock()

    class ObservedLock:
        arrivals = 0

        async def __aenter__(self):
            self.arrivals += 1
            if self.arrivals == 2:
                both_arrived.set()
            await lock.acquire()

        async def __aexit__(self, *args):
            lock.release()

    oauth.locks[oauth.credential_store.key(row.id, user)] = ObservedLock()
    original = oauth.exchange

    async def exchange(auth, data):
        await asyncio.wait_for(both_arrived.wait(), 5)
        return await original(auth, data)

    monkeypatch.setattr(oauth, "exchange", exchange)
    results = await asyncio.gather(*[oauth.credentials(row, user, force_refresh=True) for _ in range(2)])
    assert [result["access_token"] for result in results] == ["renewed", "renewed"]
    assert observed["refreshes"] == 1


async def test_first_call_recovers_catalog_after_another_session_renews_token(db_app, monkeypatch):
    row, user, observed = await expired_catalog(db_app, monkeypatch)
    await db_app.app.state.oauth.credentials(row, user)
    context, _ = await gateway_context(db_app)
    result = await invoke(db_app, context, "gateway_call", {
        "name": alias(row.slug, "whoami"), "arguments": {},
    })
    assert not result.is_error, result
    assert observed["refreshes"] == 1 and observed["calls"] == [("whoami", {})]
