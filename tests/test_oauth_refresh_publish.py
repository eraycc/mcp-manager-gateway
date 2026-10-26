"""OAuth renewal must publish capabilities in the same scheduled job."""
import asyncio
import socket
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from starlette.responses import JSONResponse
from test_exact_route_scalability import gateway_context, invoke

from mcp_manager import app as app_module
from mcp_manager import oauth as oauth_module
from mcp_manager.catalog import alias
from mcp_manager.credentials import credential_owner
from mcp_manager.database import McpServer, set_setting


@asynccontextmanager
async def oauth_http_service(h, monkeypatch, ownership, *, before_callback=None):
    """Use real token POSTs, client bindings, SDK startup and streaming discovery."""
    observed = {"refreshes": [], "lists": 0, "business": 0}
    token_number = [0]
    clock = [time.time()]

    async def tools(ctx, params):
        observed["lists"] += 1
        return types.ListToolsResult(tools=[types.Tool(name="whoami", inputSchema={"type": "object"})])

    async def call(ctx, params):
        observed["business"] += 1
        return types.CallToolResult(content=[types.TextContent(type="text", text="ok")])

    protocol = Server("oauth-publish", on_list_tools=tools, on_call_tool=call)
    downstream = protocol.streamable_http_app()

    async def protected(scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/token":
                body = bytearray()
                while True:
                    frame = await receive()
                    body.extend(frame.get("body", b""))
                    if not frame.get("more_body"):
                        break
                data = parse_qs(body.decode())
                assert data["client_id"] == ["registered-client"]
                assert data["client_secret"] == ["fixture-secret"]
                if data["grant_type"] == ["refresh_token"]:
                    expected = f"refresh-{token_number[0]}"
                    assert data["refresh_token"] == [expected]
                    observed["refreshes"].append(expected)
                    token_number[0] += 1
                else:
                    assert data["grant_type"] == ["authorization_code"]
                await JSONResponse({
                    "access_token": f"access-{token_number[0]}",
                    "refresh_token": f"refresh-{token_number[0]}",
                    "expires_in": 3600 if token_number[0] else 0,
                    "token_type": "Bearer",
                })(scope, receive, send)
                return
            if dict(scope["headers"]).get(b"authorization") != f"Bearer access-{token_number[0]}".encode():
                await JSONResponse({"error": "expired"}, 401)(scope, receive, send)
                return
        await downstream(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(protected, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)

        from mcp_manager import oauth_discovery

        async def discover(resource, auth):
            return {"issuer_url": base, "authorization_url": base + "/authorize",
                    "token_url": base + "/token", "registration_url": base + "/register",
                    "resource": resource, "cimd_supported": False, "scopes": ["read"], "iss_required": True}

        async def register(context, name, redirect):
            return {"client_id": "registered-client", "client_secret": "fixture-secret",
                    "token_endpoint_auth_method": "client_secret_post"}

        monkeypatch.setattr(oauth_discovery, "discover", discover)
        monkeypatch.setattr(oauth_discovery, "register", register)
        monkeypatch.setattr(oauth_module, "time", SimpleNamespace(time=lambda: clock[0]))
        monkeypatch.setattr(app_module, "now", lambda: datetime.fromtimestamp(clock[0], UTC))
        user = h.actor["id"]
        row = await h.app.state.catalog.create({
            "name": "HTTP OAuth", "transport": "streamable-http", "mode": "disabled",
            "isolation": "service" if ownership == "service" else "user",
            "config": {"url": base + "/mcp", "startup_timeout": 5, "call_timeout": 5, "stop_timeout": 2,
                       "auth": {"type": "oauth", "auth_mode": "dcr_only", "client_name": "Fixture",
                                "token_endpoint_auth_method": "client_secret_post",
                                "config_isolation": "user" if ownership == "private" else "shared"}},
        }, user_id=user, discover=False)
        response = await h.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
        assert response.status_code == 200, response.text
        params = parse_qs(urlsplit(response.json()["authorization_url"]).query)
        if before_callback:
            await before_callback(row, user)
            async with h.db.locked() as session:
                (await session.get(McpServer, row.id)).mode = "lazy"
        response = await h.web.get("/api/v1/oauth/callback", params={
            "state": params["state"][0], "code": "fixture-code", "iss": base})
        assert response.status_code == 303, response.text
        async with h.db.locked() as session:
            (await session.get(McpServer, row.id)).mode = "lazy"
        row = await h.app.state.catalog.get(row.id)
        await set_setting(h.db, "refresh_enabled", True)
        yield row, user, clock, observed
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()


async def fire_schedule(h):
    state = h.app.state
    state.refresh_next = app_module.now()
    await app_module._scheduled_refresh(h.app)
    identity, task = next(iter(state.jobs.tasks.items()))
    await asyncio.wait_for(task, 20)
    return await state.jobs.store.get_status(identity), await state.jobs.store.list_results(identity)


@pytest.mark.parametrize("ownership,entrypoint", [
    (ownership, entrypoint)
    for entrypoint in ("scheduled", "manual", "gateway", "personal")
    for ownership in ("service", "shared", "private")
    if entrypoint != "personal" or ownership != "service"
])
@pytest.mark.parametrize("rotation_at", ["none", "begin_refresh", "publish"])
async def test_oauth_refresh_publishes_after_late_credential_rotation(
    db_app, monkeypatch, ownership, entrypoint, rotation_at,
):
    async with oauth_http_service(db_app, monkeypatch, ownership) as (row, user, clock, observed):
        catalog = db_app.app.state.catalog
        owner = credential_owner(row.isolation, user)
        rotations = 0
        if rotation_at != "none":
            original = getattr(catalog.index, rotation_at)

            async def rotate(*args, **kwargs):
                nonlocal rotations
                if rotations == 0:
                    rotations += 1
                    # Another session committed a new credential generation after preparation.
                    # The provider may return the same access token during a refresh.
                    async with db_app.db.locked() as session:
                        payload = await db_app.app.state.credentials.load(row.id, owner, session=session)
                        await db_app.app.state.credentials.save_oauth_token(
                            session, row, user, payload["oauth_token"])
                return await original(*args, **kwargs)

            monkeypatch.setattr(catalog.index, rotation_at, rotate)

        context, _ = await gateway_context(db_app)
        for round_number in (1, 2):
            rotations = 0
            if entrypoint == "scheduled":
                job, page = await fire_schedule(db_app)
                assert job["status"] == "completed", [item.get("error") for item in page["items"]]
                assert job["total"] == job["succeeded"] == 1 and job["failed"] == 0
            elif entrypoint == "gateway":
                response = await invoke(db_app, context, "gateway_call", {
                    "name": alias(row.slug, "whoami"), "arguments": {},
                })
                assert not response.is_error, response
            else:
                path = "credentials/refresh" if entrypoint == "personal" else "refresh"
                response = await db_app.web.post(f"/api/v1/mcps/{row.id}/{path}",
                                                json={"revision": row.revision})
                assert response.status_code == 200, response.text
            cached = catalog.cached(row, user)
            assert cached["cache_status"] == "ready" and cached["cache_at"]
            assert cached["last_refresh_error_code"] is None
            capabilities = await db_app.web.get(f"/api/v1/mcps/{row.id}/capabilities")
            assert capabilities.status_code == 200, capabilities.text
            assert [entry["name"] for entry in capabilities.json()["items"]] == ["whoami"]
            token = (await db_app.app.state.credentials.load(row.id, owner))["oauth_token"]
            assert token["refresh_token"] == f"refresh-{round_number}"
            assert len(observed["refreshes"]) == round_number
            assert observed["business"] == (round_number if entrypoint == "gateway" else 0)
            clock[0] += 7200



async def test_persistent_scope_conflicts_stop_after_one_read_only_retry(db_app, monkeypatch):
    async with oauth_http_service(db_app, monkeypatch, "service") as (row, user, _clock, observed):
        catalog = db_app.app.state.catalog
        original = catalog.index.begin_refresh
        attempts = 0

        async def rotate(scope):
            nonlocal attempts
            attempts += 1
            async with db_app.db.locked() as session:
                payload = await db_app.app.state.credentials.load(row.id, "service", session=session)
                await db_app.app.state.credentials.save_oauth_token(
                    session, row, user, payload["oauth_token"])
            return await original(scope)

        monkeypatch.setattr(catalog.index, "begin_refresh", rotate)
        job, page = await fire_schedule(db_app)
        assert job["status"] == "completed_with_errors" and job["failed"] == 1
        assert page["items"][0]["error"] == "Catalog credential scope changed"
        assert attempts == 2 and observed["refreshes"] == ["refresh-0"]
        assert observed["business"] == 0
        assert catalog.cached(row, user)["cache_status"] != "ready"
        assert not db_app.app.state.runtime.leases


async def test_revoked_owner_is_not_retried_under_new_credentials(db_app, monkeypatch):
    from mcp_manager.database import User

    async with oauth_http_service(db_app, monkeypatch, "private") as (row, user, _clock, observed):
        catalog = db_app.app.state.catalog
        original = catalog.index.publish

        async def revoke(*args, **kwargs):
            async with db_app.db.locked() as session:
                (await session.get(User, user)).disabled = True
                payload = await db_app.app.state.credentials.load(row.id, user, session=session)
                await db_app.app.state.credentials.save_oauth_token(
                    session, row, user, payload["oauth_token"])
            return await original(*args, **kwargs)

        monkeypatch.setattr(catalog.index, "publish", revoke)
        job, _page = await fire_schedule(db_app)
        assert job["status"] == "completed_with_errors" and job["failed"] == 1
        assert observed["refreshes"] == ["refresh-0"] and observed["lists"] == 1
        assert observed["business"] == 0
        assert catalog.cached(row, user)["cache_status"] != "ready"


async def test_authorization_callback_uses_same_catalog_publication_retry(db_app, monkeypatch):
    catalog = db_app.app.state.catalog

    async def before_callback(row, user):
        original = catalog.index.publish
        rotated = False

        async def rotate(*args, **kwargs):
            nonlocal rotated
            if not rotated:
                rotated = True
                async with db_app.db.locked() as session:
                    payload = await db_app.app.state.credentials.load(row.id, "service", session=session)
                    await db_app.app.state.credentials.save_oauth_token(
                        session, row, user, payload["oauth_token"])
            return await original(*args, **kwargs)

        monkeypatch.setattr(catalog.index, "publish", rotate)

    async with oauth_http_service(db_app, monkeypatch, "service", before_callback=before_callback) as (
        row, user, _clock, observed,
    ):
        cache = catalog.cached(row, user)
        assert cache["cache_status"] == "ready" and cache["cache_at"]
        summary = await catalog.index.get_summary(await catalog.index_scope(row, user))
        assert summary["tool_count"] == 1
        assert observed["refreshes"] == ["refresh-0"] and observed["business"] == 0
