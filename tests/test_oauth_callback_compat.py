"""Configured callback paths preserve session, issuer and credential bindings."""
import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from mcp_manager import oauth_discovery
from mcp_manager.oauth_config import callback_uri
from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("public_url,mode,path", [
    ("http://127.0.0.1:8765", "auto", None),
    ("http://localhost:8765", "dcr", None),
    ("http://[::1]:8765", "auto", "/callback"),
    ("http://127.0.0.1:8765", "manual", "/callback"),
    ("https://gateway.example.com", "auto", "/callback"),
    ("https://gateway.example.com", "manual", "/api/v1/oauth/callback"),
])
def test_callback_path_is_explicit_and_independent_of_provider_and_mode(public_url, mode, path):
    auth = {"auth_mode": mode}
    if path is not None:
        auth["callback_path"] = path
    assert callback_uri(public_url, auth) == public_url + (path or "/api/v1/oauth/callback")


async def figma_server(harness, monkeypatch):
    harness.app.state.config.public_url = "http://127.0.0.1:8765"
    row = await harness.app.state.catalog.create({
        "name": "Figma callback", "transport": "streamable-http", "mode": "disabled", "isolation": "user",
        "config": {"url": "https://mcp.figma.com/mcp", "auth": {
            "type": "oauth", "auth_mode": "auto", "allow_dcr": True, "client_name": "Codex",
            "callback_path": "/callback"}},
    }, user_id=harness.actor["id"])
    registrations, token_requests = [], []

    async def discover(resource, auth):
        return {"resource": resource, "issuer_url": "https://api.figma.com",
            "authorization_url": "https://www.figma.com/oauth/mcp",
            "token_url": "https://api.figma.com/v1/oauth/token",
            "registration_url": "https://api.figma.com/v1/oauth/mcp/register",
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "scopes": ["mcp:connect"], "cimd_supported": False, "iss_required": True}

    def register(request):
        body = json.loads(request.content)
        registrations.append(body)
        if body["redirect_uris"] != ["http://127.0.0.1:8765/callback"]:
            return httpx.Response(400, json={"error": "invalid_redirect_uri"})
        return httpx.Response(200, json={"client_id": "figma-client", "client_secret": "sealed-secret",
                                       "token_endpoint_auth_method": "client_secret_post"})

    async def exchange(auth, data):
        assert auth["client_secret"] == "sealed-secret"
        assert data["redirect_uri"] == "http://127.0.0.1:8765/callback"
        token_requests.append(data)
        return {"access_token": "figma-access", "refresh_token": "figma-refresh", "expires_at": None}

    monkeypatch.setattr(oauth_discovery, "discover", discover)
    monkeypatch.setattr(oauth_discovery, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(register)))
    monkeypatch.setattr(harness.app.state.oauth, "exchange", exchange)
    return row, registrations, token_requests


async def test_figma_registration_callback_cache_and_runtime_share_the_same_redirect(db_app, monkeypatch):
    row, registrations, token_requests = await figma_server(db_app, monkeypatch)
    for _ in range(2):
        start = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
        assert start.status_code == 200, start.text
    assert len(registrations) == 1
    query = parse_qs(urlsplit(start.json()["authorization_url"]).query)
    assert query["redirect_uri"] == ["http://127.0.0.1:8765/callback"]
    assert query["scope"] == ["mcp:connect"] and query["code_challenge_method"] == ["S256"]
    response = await db_app.web.get("/callback", params={
        "state": query["state"][0], "code": "authorization-code", "iss": "https://api.figma.com"})
    assert response.status_code == 303, response.text
    assert len(token_requests) == 1
    status = await db_app.web.get(f"/api/v1/mcps/{row.id}/oauth/status")
    assert status.json()["authorized"]
    summaries = await db_app.app.state.credentials.configuration_summaries([row], db_app.actor["id"])
    assert summaries[row.id]["current_user_configured"]
    assert summaries[row.id]["configured_users"] == 1
    value = await db_app.app.state.oauth.credentials(row, db_app.actor["id"])
    assert value["access_token"] == "figma-access"
    row.mode = "lazy"  # Building a spec checks bindings without starting the downstream MCP.
    spec = await db_app.app.state.catalog.spec(row, db_app.actor["id"])
    assert spec.config["auth"]["access_token"] == "figma-access"


@pytest.mark.parametrize("failure", ["missing_issuer", "wrong_issuer", "wrong_state", "other_session", "expired", "changed_base"])
async def test_callback_alias_retains_authorization_guards(db_app, monkeypatch, failure):
    row, _, token_requests = await figma_server(db_app, monkeypatch)
    start = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert start.status_code == 200, start.text
    query = parse_qs(urlsplit(start.json()["authorization_url"]).query)
    state = query["state"][0]
    params = {"state": state, "code": "code", "iss": "https://api.figma.com"}
    if failure == "missing_issuer":
        params.pop("iss")
    elif failure == "wrong_issuer":
        params["iss"] = "https://wrong.test"
    elif failure == "wrong_state":
        params["state"] = "unknown-state"
    elif failure == "expired":
        db_app.app.state.oauth.pending[state]["expires"] = 0
    elif failure == "changed_base":
        db_app.app.state.config.public_url = "http://127.0.0.1:9999"
    if failure == "other_session":
        async with httpx.AsyncClient(transport=httpx.ASGITransport(db_app.app), base_url="http://test") as anonymous:
            response = await anonymous.get("/callback", params=params)
    else:
        response = await db_app.web.get("/callback", params=params)
    assert response.status_code in {401, 403, 409}, response.text
    assert not token_requests


@pytest.mark.parametrize("error", ["invalid_redirect_uri", "invalid_client_metadata", "secret-echo", [], None])
async def test_registration_failure_exposes_only_safe_error_code(error):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        400, json={"error": error, "error_description": "client_secret=never-echo-this",
                   "client_secret": "never-echo-this"}))) as client:
        with pytest.raises(GatewayError) as caught:
            await oauth_discovery.json_request(client, "https://auth.test/register", "https://mcp.test/mcp", data={})
    assert caught.value.code == "oauth_registration_failed"
    assert "HTTP 400" in str(caught.value) and "never-echo-this" not in str(caught.value)
    if isinstance(error, str) and error in {"invalid_redirect_uri", "invalid_client_metadata"}:
        assert error in str(caught.value)
    else:
        assert "secret-echo" not in str(caught.value)


@pytest.mark.parametrize("body", [b"", b"<html>not JSON</html>", b"[]", b"x" * (oauth_discovery.MAX_JSON_BYTES + 1)],
                         ids=["empty", "html", "array", "oversized"])
async def test_unstructured_or_oversized_error_body_preserves_safe_http_failure(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(400, content=body))) as client:
        with pytest.raises(GatewayError) as caught:
            await oauth_discovery.json_request(client, "https://auth.test/register", "https://mcp.test/mcp", data={})
    assert caught.value.code == "oauth_registration_failed" and "HTTP 400" in str(caught.value)


async def test_error_body_timeout_keeps_the_already_known_registration_failure(monkeypatch):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(.1)
            yield b'{"error":"invalid_redirect_uri"}'

    monkeypatch.setattr(oauth_discovery, "REQUEST_TIMEOUT", .01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(400, stream=SlowBody()))) as client:
        with pytest.raises(GatewayError) as caught:
            await oauth_discovery.json_request(client, "https://auth.test/register", "https://mcp.test/mcp", data={})
    assert caught.value.code == "oauth_registration_failed" and "HTTP 400" in str(caught.value)


@pytest.mark.parametrize("mode", ["manual", "auto", None])
@pytest.mark.parametrize("path", ["https://other.test/callback", "//other.test/callback", "/other", "/callback?x=1",
                                   "/callback#fragment", "callback", None, []])
def test_unhandled_or_external_callback_paths_are_rejected(mode, path):
    from mcp_manager.transports import validate_config

    auth = {"type": "oauth", "client_id": "registered", "authorization_url": "https://auth.test/a",
            "token_url": "https://auth.test/t", "callback_path": path}
    if mode:
        auth["auth_mode"] = mode
    if mode == "auto":
        auth.pop("client_id")
    with pytest.raises(ValueError, match="callback_path"):
        validate_config("streamable-http", {"url": "https://mcp.test/mcp", "auth": auth})


@pytest.mark.parametrize("path", ["/api/v1/oauth/callback", "/callback"])
async def test_manual_registration_uses_the_selected_callback_without_discovery(db_app, monkeypatch, path):
    from test_oauth_registration import server

    row = await server(db_app, {"type": "oauth", "auth_mode": "manual", "client_id": "pre-registered",
        "authorization_url": "https://auth.test/a", "token_url": "https://auth.test/t", "callback_path": path})
    exchanged = []

    async def forbidden_discovery(*args):
        pytest.fail("A pre-registered client with explicit endpoints must not discover or register a new client")

    async def exchange(auth, data):
        assert data["redirect_uri"] == db_app.app.state.config.public_url + path
        exchanged.append(data)
        return {"access_token": "manual-access", "expires_at": None}

    monkeypatch.setattr(oauth_discovery, "discover", forbidden_discovery)
    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    started = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert started.status_code == 200, started.text
    params = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    assert params["redirect_uri"] == [db_app.app.state.config.public_url + path]
    callback = await db_app.web.get(path, params={"state": params["state"][0], "code": "manual-code"})
    assert callback.status_code == 303, callback.text
    assert len(exchanged) == 1

@pytest.mark.parametrize("config_isolation", ["shared", "user"])
async def test_callback_path_change_invalidates_only_its_configuration_scope(db_app, monkeypatch, config_isolation):
    from test_oauth_registration import discovery, server

    calls = discovery(monkeypatch, cimd=False)
    row = await server(db_app, {"type": "oauth", "auth_mode": "auto", "allow_dcr": True, "client_name": "Codex",
                               "config_isolation": config_isolation})
    endpoint = f"/api/v1/mcps/{row.id}/oauth/start"
    started = await db_app.web.post(endpoint)
    assert started.status_code == 200, started.text
    old_state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
    owner = db_app.actor["id"]
    async with db_app.db.locked() as session:
        for identity in (owner, "other-owner"):
            await db_app.app.state.credentials.save_oauth_token(session, row, identity,
                                                               {"access_token": "previous-access", "expires_at": None})
    config = await db_app.app.state.credentials.materialize(row, owner)
    config["auth"]["callback_path"] = "/callback"
    row = await db_app.app.state.catalog.update(row.id, {"config": config}, user_id=owner)
    for identity in (owner, "other-owner"):
        payload = await db_app.app.state.credentials.load(row.id, identity)
        assert bool(payload.get("oauth_token")) == (config_isolation == "user" and identity != owner)
    payload = await db_app.app.state.credentials.load(row.id, owner if config_isolation == "user" else "service")
    assert not payload.get("oauth_registration")

    async def forbidden_exchange(*args):
        pytest.fail("A callback with the previous path/configuration must not exchange its code")

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", forbidden_exchange)
    rejected = await db_app.web.get("/api/v1/oauth/callback", params={
        "state": old_state, "code": "old-code", "iss": "https://auth.test"})
    assert rejected.status_code in {403, 409}
    fresh = await db_app.web.post(endpoint)
    assert fresh.status_code == 200, fresh.text
    assert parse_qs(urlsplit(fresh.json()["authorization_url"]).query)["redirect_uri"][0].endswith("/callback")
    assert calls == [("Codex", "http://127.0.0.1:8765/api/v1/oauth/callback"),
                     ("Codex", "http://127.0.0.1:8765/callback")]


async def test_explicit_default_callback_preserves_existing_registration_and_tokens(db_app, monkeypatch):
    from test_oauth_registration import discovery, server

    calls = discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    endpoint = f"/api/v1/mcps/{row.id}/oauth/start"
    assert (await db_app.web.post(endpoint)).status_code == 200
    owner = db_app.actor["id"]
    async with db_app.db.locked() as session:
        await db_app.app.state.credentials.save_oauth_token(session, row, owner,
                                                           {"access_token": "preserved-access", "expires_at": None})
    config = await db_app.app.state.credentials.materialize(row, owner)
    config["auth"]["callback_path"] = "/api/v1/oauth/callback"
    row = await db_app.app.state.catalog.update(row.id, {"config": config}, user_id=owner)
    assert (await db_app.app.state.credentials.load(row.id, owner))["oauth_token"]["access_token"] == "preserved-access"
    assert (await db_app.web.post(endpoint)).status_code == 200
    assert len(calls) == 1
