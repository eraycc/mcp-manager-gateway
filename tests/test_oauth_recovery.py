"""OAuth recovery preserves token clients and isolates authorization-flow resets."""
import asyncio
import copy
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_oauth_registration import discovery, server

from mcp_manager.database import McpServer, SystemSetting, User
from mcp_manager.runtime import GatewayError


def flow(response):
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["authorization_url"]).query)


async def begin(h, row, web=None):
    return flow(await (web or h.web).post(f"/api/v1/mcps/{row.id}/oauth/start"))


async def reset(h, row, web=None):
    response = await (web or h.web).post(f"/api/v1/mcps/{row.id}/oauth/reset")
    assert response.status_code == 200, response.text
    return response.json()


async def test_reset_endpoint_clears_pending_and_reports_result(db_app, monkeypatch):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    state = (await begin(db_app, row))["state"][0]
    result = await reset(db_app, row)
    assert result["cleared_states"] == 1 and result["cleared_client"] is True
    assert isinstance(result["message"], str) and result["message"]
    assert state not in db_app.app.state.oauth.pending


async def test_new_start_replaces_previous_state_in_same_scope(db_app, monkeypatch):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    first = await begin(db_app, row)
    second = await begin(db_app, row)
    assert first["state"] != second["state"]
    pending = db_app.app.state.oauth.pending
    assert first["state"][0] not in pending
    assert second["state"][0] in pending


@pytest.fixture
def audit_events(db_app, monkeypatch):
    events = []

    async def audit(action, actor, details):
        events.append((action, actor, copy.deepcopy(details)))

    monkeypatch.setattr(db_app.app.state.catalog.logs, "audit", audit)
    return events


@asynccontextmanager
async def actor(h, name, *, role="user", allowed=True):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(h.app), base_url="http://test") as web:
        response = await web.post("/api/v1/auth/register", json={"username": name, "password": "password12345"})
        assert response.status_code == 200, response.text
        identity = response.json()["id"]
        async with h.db.locked() as session:
            user = await session.get(User, identity)
            user.role = role
            user.scope_mode = "all" if allowed else "selected"
            user.mcp_ids = []
        response = await web.post("/api/v1/auth/login", json={"username": name, "password": "password12345"})
        assert response.status_code == 200, response.text
        web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
        yield web, identity


async def shared_server(h):
    return await h.app.state.catalog.create({
        "name": "Shared recovery", "transport": "streamable-http", "mode": "disabled", "isolation": "service",
        "config": {"url": "https://mcp.test/mcp",
                   "auth": {"type": "oauth", "auth_mode": "dcr_only", "client_name": "Codex"}},
    }, user_id=h.actor["id"])


async def callback(h, state, web=None, **params):
    return await (web or h.web).get("/api/v1/oauth/callback",
        params={"state": state, "code": "code", "iss": "https://auth.test", **params})


def failed_event(events, row, state=None):
    matches = [details for action, _, details in events
               if action == "oauth.callback_failed" and details.get("mcp_id") == row.id]
    assert matches, events
    result = matches[-1]
    assert result.get("reason")
    if state is not None:
        assert result.get("state_prefix") == state[:8]
        assert state not in repr(result)
    return result


def invalidated_event(events, row):
    matches = [details for action, _, details in events
               if action == "oauth.state_invalidated" and details.get("mcp_id") == row.id]
    assert matches, events
    result = matches[-1]
    assert result["reason"] == "config_changed"
    assert result["cleared_states"] >= 1
    return result


async def test_personal_reset_and_start_preserve_other_user_pending(db_app, monkeypatch):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    mine = (await begin(db_app, row))["state"][0]
    async with actor(db_app, "other-user") as (web, _):
        theirs = (await begin(db_app, row, web))["state"][0]
        mine2 = (await begin(db_app, row))["state"][0]
        assert mine not in db_app.app.state.oauth.pending
        assert theirs in db_app.app.state.oauth.pending
        result = await reset(db_app, row)
        assert result["cleared_states"] == 1
        assert mine2 not in db_app.app.state.oauth.pending
        assert theirs in db_app.app.state.oauth.pending


async def test_shared_reset_and_start_apply_to_whole_service_scope(db_app, monkeypatch):
    discovery(monkeypatch, cimd=False)
    row = await shared_server(db_app)
    first = (await begin(db_app, row))["state"][0]
    async with actor(db_app, "second-admin", role="admin") as (web, _):
        second = (await begin(db_app, row, web))["state"][0]
        assert first not in db_app.app.state.oauth.pending
        assert second in db_app.app.state.oauth.pending
        result = await reset(db_app, row)
        assert result["cleared_states"] == 1
        assert second not in db_app.app.state.oauth.pending


@pytest.mark.parametrize("allowed", [True, False])
async def test_user_cannot_reset_shared_service(db_app, monkeypatch, allowed):
    discovery(monkeypatch, cimd=False)
    row = await shared_server(db_app)
    state = (await begin(db_app, row))["state"][0]
    before = await db_app.app.state.credentials.load(row.id, "service")
    async with actor(db_app, "non-admin", allowed=allowed) as (web, _):
        response = await web.post(f"/api/v1/mcps/{row.id}/oauth/reset")
        assert response.status_code in {403, 404}, response.text
    assert state in db_app.app.state.oauth.pending
    assert await db_app.app.state.credentials.load(row.id, "service") == before


@pytest.mark.parametrize("shared", [False, True])
async def test_reset_keeps_existing_token_epoch_and_cimd_evidence(db_app, monkeypatch, shared):
    discovery(monkeypatch, cimd=False)
    row = await shared_server(db_app) if shared else await server(db_app)
    await begin(db_app, row)
    state = db_app.app.state
    user_id = db_app.actor["id"]
    token_owner = "service" if shared else user_id
    async with db_app.db.locked() as session:
        await state.credentials.save_oauth_token(session, row, user_id,
            {"access_token": "old-access", "refresh_token": "old-refresh", "expires_at": time.time() + 3600})
        session.add(SystemSetting(key=state.oauth.cimd.setting_key(user_id),
                                  value=state.catalog.seal({"evidence": "keep-exactly"})))
    before = await state.credentials.load(row.id, token_owner)
    await reset(db_app, row)
    after = await state.credentials.load(row.id, token_owner)
    assert after["oauth_token"] == before["oauth_token"]
    assert after["oauth_epoch"] == before["oauth_epoch"]
    async with db_app.db.locked() as session:
        evidence = await session.get(SystemSetting, state.oauth.cimd.setting_key(user_id))
        assert state.catalog.unseal(evidence.value) == {"evidence": "keep-exactly"}


async def test_personal_reset_preserves_shared_registration_and_old_refresh_client(db_app, monkeypatch):
    from mcp_manager import oauth_discovery

    discovery(monkeypatch, cimd=False)
    clients = []

    async def register(context, name, redirect):
        client_id = "client-" + str(len(clients) + 1)
        clients.append(client_id)
        return {"client_id": client_id, "client_secret": client_id + "-secret",
                "token_endpoint_auth_method": "client_secret_post"}

    monkeypatch.setattr(oauth_discovery, "register", register)
    row = await server(db_app)
    state = db_app.app.state
    user_id = db_app.actor["id"]
    initial = await begin(db_app, row)

    async def exchange(auth, data):
        assert auth["client_id"] == "client-1"
        assert auth["client_secret"] == "client-1-secret"
        if data["grant_type"] == "refresh_token":
            assert data["refresh_token"] == "refresh-for-client-1"
            return {"access_token": "renewed", "expires_at": time.time() + 3600}
        return {"access_token": "original", "refresh_token": "refresh-for-client-1",
                "expires_at": time.time() - 10}

    monkeypatch.setattr(state.oauth, "exchange", exchange)
    assert (await callback(db_app, initial["state"][0])).status_code == 303
    async with db_app.db.locked() as session:
        await state.credentials.save_oauth_token(session, row, "other-owner",
            {"access_token": "other-access", "refresh_token": "other-refresh", "expires_at": None})
    service_before = await state.credentials.load(row.id, "service")
    other_before = await state.credentials.load(row.id, "other-owner")
    mine_before = await state.credentials.load(row.id, user_id)
    await reset(db_app, row)
    assert await state.credentials.load(row.id, "service") == service_before
    assert await state.credentials.load(row.id, "other-owner") == other_before
    restarted = await begin(db_app, row)
    assert restarted["client_id"] == ["client-2"]
    assert await state.credentials.load(row.id, "service") == service_before
    assert await state.credentials.load(row.id, "other-owner") == other_before
    mine = await state.credentials.load(row.id, user_id)
    assert mine["oauth_token"] == mine_before["oauth_token"]
    assert mine["oauth_epoch"] == mine_before["oauth_epoch"]
    refreshed = await state.oauth.credentials(row, user_id)
    assert refreshed["access_token"] == "renewed"


@pytest.mark.parametrize("path", ["/api/v1/oauth/callback", "/callback"])
async def test_unauthenticated_callback_audits_without_consuming_state(db_app, monkeypatch, audit_events, path):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    state = (await begin(db_app, row))["state"][0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(db_app.app), base_url="http://test") as web:
        response = await web.get(path, params={"state": state, "code": "code"})
    assert response.status_code == 401
    assert state in db_app.app.state.oauth.pending
    details = failed_event(audit_events, row, state)
    assert details["reason"] == "state_mismatch"


@pytest.mark.parametrize("kind", ["missing", "mismatch", "expired", "used"])
async def test_state_failure_is_403_classified_and_single_use(db_app, monkeypatch, audit_events, kind):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    initial = await begin(db_app, row)
    state = initial["state"][0]

    async def exchange(auth, data):
        return {"access_token": "authorized", "expires_at": None}

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    if kind == "expired":
        db_app.app.state.oauth.pending[state]["expires"] = time.time() - 1
    if kind == "used":
        assert (await callback(db_app, state)).status_code == 303
    if kind == "missing":
        response = await db_app.web.get("/api/v1/oauth/callback", params={"code": "code"})
    else:
        response = await callback(db_app, "unknown-state-value" if kind == "mismatch" else state)
    assert response.status_code == 403, response.text
    assert "state_" + kind in response.text, response.text
    if kind in {"expired", "used"}:
        details = failed_event(audit_events, row, state)
        assert details["reason"] == "state_" + kind or (
            kind == "used" and details["reason"] == "state_mismatch" and details.get("state_reason") == "state_used")


async def test_wrong_session_does_not_consume_legitimate_pending(db_app, monkeypatch, audit_events):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    state = (await begin(db_app, row))["state"][0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(db_app.app), base_url="http://test") as web:
        login = await web.post("/api/v1/auth/login", json={"username": "admin", "password": "password12345"})
        assert login.status_code == 200
        denied = await callback(db_app, state, web)
        assert denied.status_code == 403
    assert state in db_app.app.state.oauth.pending

    async def exchange(auth, data):
        return {"access_token": "legitimate", "expires_at": None}

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    assert (await callback(db_app, state)).status_code == 303


@pytest.mark.parametrize("kind", ["issuer", "exchange", "provider"])
async def test_failed_callback_consumes_matching_state_and_audits(db_app, monkeypatch, audit_events, kind):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    state = (await begin(db_app, row))["state"][0]

    async def exchange(auth, data):
        raise GatewayError("oauth_failed", "exchange refused")

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    params = {"iss": "https://wrong.test"} if kind == "issuer" else (
        {"error": "access_denied"} if kind == "provider" else {})
    response = await callback(db_app, state, **params)
    assert response.status_code >= 400
    assert state not in db_app.app.state.oauth.pending
    details = failed_event(audit_events, row, state)
    if kind == "exchange":
        assert details["reason"] == "code_exchange_failed"
    elif kind == "issuer":
        assert details["reason"] == "state_mismatch"
        assert details.get("state_reason") == "issuer_mismatch"


@pytest.mark.parametrize("field,value", [
    ("scopes", ["read", "write"]), ("callback_path", "/callback"), ("auth_mode", "cimd_only"),
])
async def test_service_config_update_invalidates_pending_and_audits(db_app, monkeypatch, audit_events, field, value):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    state = (await begin(db_app, row))["state"][0]
    config = await db_app.app.state.credentials.materialize(row, db_app.actor["id"], require=False)
    config["auth"][field] = value
    response = await db_app.web.patch(f"/api/v1/mcps/{row.id}", json={"config": config})
    assert response.status_code == 200, response.text
    response = await callback(db_app, state)
    assert response.status_code in {403, 409}, response.text
    assert state not in db_app.app.state.oauth.pending
    invalidated_event(audit_events, row)
    assert failed_event(audit_events, row, state)["reason"] == "state_mismatch"


async def test_public_url_change_invalidates_pending_on_status(db_app, monkeypatch, audit_events):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    state = (await begin(db_app, row))["state"][0]
    db_app.app.state.config.public_url = "http://localhost:8765"
    response = await db_app.web.get(f"/api/v1/mcps/{row.id}/oauth/status")
    assert response.status_code == 200
    assert state not in db_app.app.state.oauth.pending
    invalidated_event(audit_events, row)


async def test_reset_blocks_inflight_start_from_publishing_old_generation(db_app, monkeypatch):
    from mcp_manager import oauth_discovery

    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    original = oauth_discovery.discover
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(resource, auth):
        entered.set()
        await release.wait()
        return await original(resource, auth)

    monkeypatch.setattr(oauth_discovery, "discover", delayed)
    task = asyncio.create_task(db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(reset(db_app, row), 5)
    finally:
        release.set()
        response = await task
    assert response.status_code in {403, 409}, response.text
    assert not any(item["server_id"] == row.id for item in db_app.app.state.oauth.pending.values())


async def test_reset_blocks_inflight_callback_token_commit(db_app, monkeypatch):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    state = (await begin(db_app, row))["state"][0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(auth, data):
        entered.set()
        await release.wait()
        return {"access_token": "late-token", "expires_at": None}

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", delayed)
    task = asyncio.create_task(callback(db_app, state))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(reset(db_app, row), 5)
    finally:
        release.set()
    response = await task
    assert response.status_code in {403, 409}, response.text
    payload = await db_app.app.state.credentials.load(row.id, db_app.actor["id"])
    assert not payload.get("oauth_token")


async def test_personal_config_update_invalidates_own_pending(db_app, monkeypatch, audit_events):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app, {"type": "oauth", "auth_mode": "dcr_only",
                               "client_name": "Codex", "config_isolation": "user"})
    pending_state = (await begin(db_app, row))["state"][0]
    config = await db_app.app.state.credentials.materialize(row, db_app.actor["id"], require=False)
    config["auth"]["scopes"] = ["changed"]
    response = await db_app.web.put(f"/api/v1/mcps/{row.id}/credentials",
        json={"revision": row.revision, "auth": config["auth"]})
    assert response.status_code == 200, response.text
    response = await callback(db_app, pending_state)
    assert response.status_code in {403, 409}, response.text
    assert pending_state not in db_app.app.state.oauth.pending
    invalidated_event(audit_events, row)
    assert failed_event(audit_events, row, pending_state)["reason"] == "state_mismatch"


async def test_reset_does_not_discard_inflight_refresh_rotation(db_app, monkeypatch):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    pending_state = (await begin(db_app, row))["state"][0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def exchange(auth, data):
        assert auth["client_id"] == "registered-client"
        if data["grant_type"] == "authorization_code":
            return {"access_token": "expired-access", "refresh_token": "original-refresh",
                    "expires_at": time.time() - 10}
        assert data["refresh_token"] == "original-refresh"
        entered.set()
        await release.wait()
        return {"access_token": "rotated-access", "refresh_token": "rotated-refresh",
                "expires_at": time.time() + 3600}

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    assert (await callback(db_app, pending_state)).status_code == 303
    task = asyncio.create_task(db_app.app.state.oauth.credentials(row, db_app.actor["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        before = await db_app.app.state.credentials.load(row.id, db_app.actor["id"])
        await asyncio.wait_for(reset(db_app, row), 5)
        after = await db_app.app.state.credentials.load(row.id, db_app.actor["id"])
        assert after["oauth_epoch"] == before["oauth_epoch"]
    finally:
        release.set()
        refreshed = await task
    assert refreshed["refresh_token"] == "rotated-refresh"
    saved = await db_app.app.state.credentials.load(row.id, db_app.actor["id"])
    assert saved["oauth_token"]["refresh_token"] == "rotated-refresh"


@pytest.mark.parametrize("stage", ["discover", "register"])
async def test_new_start_supersedes_inflight_start_and_keeps_only_latest_state(db_app, monkeypatch, stage):
    from mcp_manager import oauth_discovery

    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    oauth = db_app.app.state.oauth
    original_prepare = oauth.prepare
    original_operation = getattr(oauth_discovery, stage)
    entered, newer_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    prepare_calls = 0
    operation_calls = 0

    async def observed_prepare(*args, **kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 2:
            newer_started.set()
        return await original_prepare(*args, **kwargs)

    async def delayed_operation(*args, **kwargs):
        nonlocal operation_calls
        operation_calls += 1
        if operation_calls == 1:
            entered.set()
            await release.wait()
        return await original_operation(*args, **kwargs)

    monkeypatch.setattr(oauth, "prepare", observed_prepare)
    monkeypatch.setattr(oauth_discovery, stage, delayed_operation)
    first = asyncio.create_task(db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
        await asyncio.wait_for(newer_started.wait(), 5)
    finally:
        release.set()
        responses = await asyncio.gather(first, *([second] if second is not None else []))
    assert len(responses) == 2
    assert responses[0].status_code in {403, 409}, responses[0].text
    latest = flow(responses[1])["state"][0]
    states = [key for key, item in oauth.pending.items() if item["server_id"] == row.id]
    assert states == [latest]
    if stage == "register":
        assert operation_calls == 1


async def test_new_start_prevents_old_inflight_callback_overwriting_new_flow(db_app, monkeypatch):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    first = (await begin(db_app, row))["state"][0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def exchange(auth, data):
        if data.get("code") == "old-code":
            entered.set()
            await release.wait()
            return {"access_token": "old-flow-token", "expires_at": None}
        assert data.get("code") == "new-code"
        return {"access_token": "new-flow-token", "expires_at": None}

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    task = asyncio.create_task(callback(db_app, first, code="old-code"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        latest = (await begin(db_app, row))["state"][0]
    finally:
        release.set()
        response = await task
    assert response.status_code in {403, 409}, response.text
    payload = await db_app.app.state.credentials.load(row.id, db_app.actor["id"])
    assert not payload.get("oauth_token")
    assert latest in db_app.app.state.oauth.pending
    assert (await callback(db_app, latest, code="new-code")).status_code == 303
    payload = await db_app.app.state.credentials.load(row.id, db_app.actor["id"])
    assert payload["oauth_token"]["access_token"] == "new-flow-token"


async def test_repeated_pending_then_one_reset_recovers_without_browser_restart(db_app, monkeypatch, audit_events):
    from mcp_manager import oauth_discovery

    discovery(monkeypatch, cimd=False)
    registrations = []

    async def register(context, name, redirect):
        identity = "stale-client" if not registrations else "fresh-client"
        registrations.append(identity)
        return {"client_id": identity, "token_endpoint_auth_method": "none"}

    monkeypatch.setattr(oauth_discovery, "register", register)
    row = await server(db_app)
    config = await db_app.app.state.credentials.materialize(row, db_app.actor["id"])
    config["url"] = "https://mcp.vercel.com"
    row = await db_app.app.state.catalog.update(row.id, {"config": config}, user_id=db_app.actor["id"])

    async def exchange(auth, data):
        if auth["client_id"] == "stale-client":
            raise GatewayError("oauth_failed", "stale registration")
        return {"access_token": "recovered", "refresh_token": "recovered-refresh", "expires_at": None}

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    first = await begin(db_app, row)
    assert (await callback(db_app, first["state"][0])).status_code == 502
    for index in range(3):
        await db_app.app.state.catalog.update(row.id, {"description": "retry " + str(index)},
                                              user_id=db_app.actor["id"])
        row = await db_app.app.state.catalog.get(row.id)
        old = await begin(db_app, row)
    assert len(db_app.app.state.oauth.pending) == 1
    assert not any(action == "oauth.authorized" for action, _, _ in audit_events)
    result = await reset(db_app, row)
    assert result["cleared_states"] == 1 and result["cleared_client"]
    fresh = await begin(db_app, row)
    assert fresh["client_id"] == ["fresh-client"]
    assert (await callback(db_app, old["state"][0])).status_code == 403
    assert (await callback(db_app, fresh["state"][0])).status_code == 303
    assert not db_app.app.state.oauth.pending
    status = (await db_app.web.get(f"/api/v1/mcps/{row.id}/oauth/status")).json()
    assert status["authorized"]
    assert registrations == ["stale-client", "fresh-client"]
    assert any(action == "oauth.reset" for action, _, _ in audit_events)
    assert any(action == "oauth.authorized" for action, _, _ in audit_events)


async def test_shared_profile_configuration_summary_is_fresh_and_has_no_secret(db_app):
    auth = {"type": "oauth", "auth_mode": "pre_registration", "client_id": "initial",
        "client_secret": "private-shared-secret", "authorization_url": "https://auth.test/a",
        "token_url": "https://auth.test/t", "scopes": ["read"]}
    row = await server(db_app, auth)
    async with db_app.db.locked() as session:
        stored = await session.get(McpServer, row.id)
        stored.mode = "lazy"
    row = await db_app.app.state.catalog.get(row.id)
    async with actor(db_app, "summary-user") as (web, _):
        response = await web.get(f"/api/v1/mcps/{row.id}/credentials/status")
        assert response.status_code == 200, response.text
        before = response.json()
        assert before["oauth_config_summary"]["client_id"] == "initial"
        config = await db_app.app.state.credentials.materialize(row, db_app.actor["id"])
        config["auth"].update(client_id="updated", scopes=["write"])
        await db_app.app.state.catalog.update(row.id, {"config": config}, user_id=db_app.actor["id"])
        response = await web.get(f"/api/v1/mcps/{row.id}/credentials/status")
        assert response.status_code == 200, response.text
        after = response.json()
        assert after["oauth_config_summary"]["client_id"] == "updated"
        assert after["oauth_config_summary"]["scopes"] == ["write"]
        assert after["revision"] > before["revision"]
        assert "private-shared-secret" not in response.text
        assert "client_secret" not in after["oauth_config_summary"]
        assert "oauth_config" not in after


async def test_private_profile_configuration_uses_personal_values(db_app):
    auth = {"type": "oauth", "auth_mode": "pre_registration", "client_id": "admin-client",
        "authorization_url": "https://auth.test/a", "token_url": "https://auth.test/t",
        "config_isolation": "user"}
    row = await server(db_app, auth)
    async with db_app.db.locked() as session:
        stored = await session.get(McpServer, row.id)
        stored.mode = "lazy"
    row = await db_app.app.state.catalog.get(row.id)
    async with actor(db_app, "private-config-user") as (web, _):
        before = (await web.get(f"/api/v1/mcps/{row.id}/credentials/status")).json()
        assert before["oauth_config"] == {}
        mine = auth | {"client_id": "my-client", "scopes": ["personal"]}
        response = await web.put(f"/api/v1/mcps/{row.id}/credentials",
            json={"revision": before["revision"], "auth": mine})
        assert response.status_code == 200, response.text
        response = await web.get(f"/api/v1/mcps/{row.id}/credentials/status")
        assert response.status_code == 200, response.text
        status = response.json()
        assert status["oauth_config"]["client_id"] == "my-client"
        assert status["oauth_config"]["scopes"] == ["personal"]
        assert "oauth_config_summary" not in status
    response = await db_app.web.get(f"/api/v1/mcps/{row.id}/credentials/status")
    assert response.status_code == 200, response.text
    admin = response.json()
    assert admin["oauth_config"]["client_id"] == "admin-client"


def test_consumed_state_history_is_bounded_and_contains_no_pkce(monkeypatch):
    from mcp_manager import oauth_states

    monkeypatch.setattr(oauth_states, "MAX_RECENT_STATES", 4)
    flows = oauth_states.OAuthFlows()
    for index in range(10):
        state = "nonce-" + str(index)
        flows.remember(state, {
            "server_id": "mcp", "user_id": "me", "session_id": "browser",
            "verifier": "private-pkce", "auth": {"client_secret": "private-client"}},
            "state_used")
    assert len(flows.recent) == 4
    assert "private-pkce" not in repr(flows.recent)
    assert "private-client" not in repr(flows.recent)
    assert all(not digest.startswith("nonce-") for digest in flows.recent)


async def test_shared_client_replacement_preserves_independently_authorized_private_token(db_app, monkeypatch):
    from mcp_manager import oauth_discovery

    discovery(monkeypatch, cimd=False)
    clients = []

    async def register(context, name, redirect):
        client_id = "client-" + str(len(clients) + 1)
        clients.append(client_id)
        return {"client_id": client_id, "client_secret": client_id + "-secret",
                "token_endpoint_auth_method": "client_secret_post"}

    async def exchange(auth, data):
        return {"access_token": "access-for-" + auth["client_id"],
                "refresh_token": "refresh-for-" + auth["client_id"], "expires_at": None}

    monkeypatch.setattr(oauth_discovery, "register", register)
    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    row = await server(db_app)
    credentials = db_app.app.state.credentials
    assert (await begin(db_app, row))["client_id"] == ["client-1"]
    await reset(db_app, row)
    private_flow = await begin(db_app, row)
    assert private_flow["client_id"] == ["client-2"]
    assert (await callback(db_app, private_flow["state"][0])).status_code == 303
    private_before = await credentials.load(row.id, db_app.actor["id"])
    assert private_before["oauth_registration_private"]
    assert private_before["oauth_token_client"]["auth"]["client_id"] == "client-2"

    async with actor(db_app, "shared-client-user") as (web, identity):
        shared_flow = await begin(db_app, row, web)
        assert shared_flow["client_id"] == ["client-1"]
        assert (await callback(db_app, shared_flow["state"][0], web)).status_code == 303
        async with db_app.db.locked() as session:
            shared = await credentials.load(row.id, "service", session=session)
            shared["oauth_registration"]["auth"]["client_secret_expires_at"] = 1
            await credentials.write(session, row.id, "service", shared)
        replacement = await begin(db_app, row, web)
        assert replacement["client_id"] == ["client-3"]
        affected = await credentials.load(row.id, identity)
        assert not affected.get("oauth_token"), "The old shared-client binding must still be invalidated"
    private_after = await credentials.load(row.id, db_app.actor["id"])
    assert private_after["oauth_token"] == private_before["oauth_token"]
    assert private_after["oauth_token_client"] == private_before["oauth_token_client"]
    assert private_after["oauth_epoch"] == private_before["oauth_epoch"]


async def test_reset_blocks_inflight_cimd_to_dcr_fallback(db_app, monkeypatch):
    from mcp_manager import oauth_discovery

    discovery(monkeypatch, cimd=True)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app)
    initial = await begin(db_app, row)
    assert initial["client_id"] == ["https://gateway.example.com/oauth/client-metadata.json"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_register(context, name, redirect):
        entered.set()
        await release.wait()
        return {"client_id": "late-fallback-client", "token_endpoint_auth_method": "none"}

    monkeypatch.setattr(oauth_discovery, "register", delayed_register)
    task = asyncio.create_task(callback(db_app, initial["state"][0], error="invalid_client"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(reset(db_app, row), 5)
    finally:
        release.set()
        response = await task
    assert response.status_code in {403, 409}, response.text
    assert not any(item["server_id"] == row.id for item in db_app.app.state.oauth.pending.values())
    shared = await db_app.app.state.credentials.load(row.id, "service")
    assert shared["oauth_registration"]["mode"] == "cimd"
    personal = await db_app.app.state.credentials.load(row.id, db_app.actor["id"])
    assert not personal.get("oauth_token")
