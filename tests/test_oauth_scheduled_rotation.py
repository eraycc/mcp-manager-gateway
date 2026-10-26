"""Exercise OAuth renewal through real entrypoints and persisted rotating tokens."""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from test_exact_route_scalability import gateway_context, invoke

from mcp_manager import app as app_module
from mcp_manager import oauth as oauth_module
from mcp_manager.catalog import alias
from mcp_manager.credentials import credential_owner
from mcp_manager.database import set_setting

TOOL = {"name": "whoami", "inputSchema": {"type": "object"}}


async def rotating_service(h, monkeypatch, ownership, *, cached=False, rotate=True):
    isolation = "service" if ownership == "service" else "user"
    user = h.actor["id"]
    catalog = h.app.state.catalog
    row = await catalog.create({
        "name": "Rotating OAuth", "transport": "streamable-http", "isolation": isolation,
        "config": {"url": "https://mcp.test/mcp", "auth": {
            "type": "oauth", "auth_mode": "pre_registration", "client_id": "test-client",
            "config_isolation": "user" if ownership == "private" else "shared",
            "authorization_url": "https://auth.test/authorize", "token_url": "https://auth.test/token"}},
    }, user_id=user, discover=False)
    async with h.db.locked() as session:
        await h.app.state.credentials.save_oauth_token(session, row, user, {
            "access_token": "access-0", "refresh_token": "refresh-0", "expires_at": 0,
        })
    if cached:
        scope = await catalog.index_scope(row, user)
        generation = await catalog.index.begin_refresh(scope)
        await catalog.index.stage_page(generation.id, "tools", [TOOL])
        await catalog.index.publish(generation.id, expected_scope=scope)
    clock = [time.time()]
    monkeypatch.setattr(oauth_module, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(app_module, "now", lambda: datetime.fromtimestamp(clock[0], UTC))
    observed = {"refreshes": [], "connections": [], "calls": []}

    def token_endpoint(request):
        data = parse_qs(request.content.decode())
        attempt = len(observed["refreshes"])
        expected = f"refresh-{attempt}" if rotate else "refresh-0"
        assert data["grant_type"] == ["refresh_token"]
        assert data["refresh_token"] == [expected], "Consumed refresh tokens must never be reused"
        assert data["client_id"] == ["test-client"]
        observed["refreshes"].append(expected)
        token = {"access_token": f"access-{attempt + 1}", "expires_in": 3600, "token_type": "Bearer"}
        if rotate:
            token["refresh_token"] = f"refresh-{attempt + 1}"
        return httpx.Response(200, json=token)

    monkeypatch.setattr(oauth_module, "token_http_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(token_endpoint)))

    class Connection:
        async def discover(self):
            return {"tools": [TOOL], "resources": [], "prompts": [], "templates": []}

        async def call(self, name, arguments):
            observed["calls"].append((name, arguments))
            return {"content": [{"type": "text", "text": "ok"}]}

    @asynccontextmanager
    async def connector(spec):
        assert spec.config["auth"]["access_token"] == f"access-{len(observed['refreshes'])}"
        observed["connections"].append(spec.config["auth"]["access_token"])
        yield Connection()

    monkeypatch.setattr(h.app.state.runtime, "connector", connector)
    return row, user, clock, observed


async def scheduled_round(h):
    state = h.app.state
    state.refresh_next = app_module.now() - timedelta(seconds=1)
    await app_module._scheduled_refresh(h.app)
    identity, task = next(iter(state.jobs.tasks.items()))
    await asyncio.wait_for(task, 15)
    job = await state.jobs.store.get_status(identity)
    page = await state.jobs.store.list_results(identity)
    assert job["status"] == "completed", page
    assert job["total"] == job["succeeded"] == 1 and job["failed"] == 0
    assert all(item["ok"] for item in page["items"]), page
    return page["items"][0]


@pytest.mark.parametrize("ownership", ["service", "shared", "private"])
@pytest.mark.parametrize("entrypoint", ["scheduled", "manual", "gateway"])
async def test_two_idle_periods_rotate_tokens_and_complete_in_one_operation(
    db_app, monkeypatch, ownership, entrypoint,
):
    row, user, clock, observed = await rotating_service(
        db_app, monkeypatch, ownership, cached=entrypoint == "gateway")
    if entrypoint == "scheduled":
        await set_setting(db_app.db, "refresh_enabled", True)
    context, _ = await gateway_context(db_app)
    for round_number in (1, 2):
        if entrypoint == "scheduled":
            result = await scheduled_round(db_app)
            target = json.loads(result["item"])
            assert target == {"server_id": row.id, "user_id": None if ownership == "service" else user}
        elif entrypoint == "manual":
            response = await db_app.web.post(f"/api/v1/mcps/{row.id}/refresh")
            assert response.status_code == 200, response.text
        else:
            response = await invoke(db_app, context, "gateway_call", {
                "name": alias(row.slug, "whoami"), "arguments": {},
            })
            assert not response.is_error, response
        payload = await db_app.app.state.credentials.load(
            row.id, credential_owner(row.isolation, user))
        assert payload["oauth_token"]["refresh_token"] == f"refresh-{round_number}"
        assert payload["oauth_token"]["access_token"] == f"access-{round_number}"
        assert not payload.get("oauth_state")
        assert len(observed["refreshes"]) == round_number
        scope = await db_app.app.state.catalog.index_scope(row, user)
        summary = await db_app.app.state.catalog.index.get_summary(scope)
        assert summary["tool_count"] == 1
        cached = db_app.app.state.catalog.cached(row, user)
        assert cached["cache_status"] == "ready" and cached["cache_at"]
        clock[0] += 7200
    assert len(observed["calls"]) == (2 if entrypoint == "gateway" else 0)


@pytest.mark.parametrize("ownership", ["shared", "private"])
async def test_profile_refresh_uses_rotated_token_after_another_idle_period(db_app, monkeypatch, ownership):
    row, user, clock, observed = await rotating_service(db_app, monkeypatch, ownership)
    for round_number in (1, 2):
        response = await db_app.web.post(f"/api/v1/mcps/{row.id}/credentials/refresh",
                                        json={"revision": row.revision})
        assert response.status_code == 200, response.text
        assert response.json()["tool_count"] == 1
        saved = await db_app.app.state.credentials.load(row.id, user)
        assert saved["oauth_token"]["refresh_token"] == f"refresh-{round_number}"
        clock[0] += 7200
    assert observed["refreshes"] == ["refresh-0", "refresh-1"] and observed["calls"] == []


async def test_refresh_response_without_replacement_preserves_refresh_token(db_app, monkeypatch):
    row, _user, clock, observed = await rotating_service(db_app, monkeypatch, "service", rotate=False)
    for _ in range(2):
        response = await db_app.web.post(f"/api/v1/mcps/{row.id}/refresh")
        assert response.status_code == 200, response.text
        saved = await db_app.app.state.credentials.load(row.id, "service")
        assert saved["oauth_token"]["refresh_token"] == "refresh-0"
        clock[0] += 7200
    assert observed["refreshes"] == ["refresh-0", "refresh-0"]
