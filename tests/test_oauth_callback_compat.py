"""Figma loopback callbacks preserve the same session, issuer and credential bindings."""
import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from mcp_manager import oauth_discovery
from mcp_manager.oauth_config import callback_uri
from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("public_url,resource,mode,path", [
    ("http://127.0.0.1:8765", "https://mcp.figma.com/mcp", "auto", "/callback"),
    ("http://localhost:8765", "https://mcp.figma.com/mcp", "dcr", "/callback"),
    ("http://[::1]:8765", "https://mcp.figma.com/mcp", "auto", "/callback"),
    ("http://127.0.0.1:8765", "https://mcp.figma.com/mcp", "manual", "/api/v1/oauth/callback"),
    ("http://127.0.0.1:8765", "https://mcp.test/mcp", "auto", "/api/v1/oauth/callback"),
    ("https://gateway.example.com", "https://mcp.figma.com/mcp", "auto", "/api/v1/oauth/callback"),
    ("http://192.168.1.2:8765", "https://mcp.figma.com/mcp", "auto", "/api/v1/oauth/callback"),
])
def test_only_figma_loopback_automatic_registration_uses_compatible_callback(public_url, resource, mode, path):
    assert callback_uri(public_url, {"auth_mode": mode}, resource) == public_url + path


async def figma_server(harness, monkeypatch):
    harness.app.state.config.public_url = "http://127.0.0.1:8765"
    row = await harness.app.state.catalog.create({
        "name": "Figma callback", "transport": "streamable-http", "mode": "disabled", "isolation": "user",
        "config": {"url": "https://mcp.figma.com/mcp", "auth": {
            "type": "oauth", "auth_mode": "auto", "allow_dcr": True, "client_name": "Codex"}},
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
