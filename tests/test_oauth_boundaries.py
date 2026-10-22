"""Discovery and registration must preserve security and ownership boundaries."""
import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_oauth_discovery import harness
from test_oauth_registration import discovery, server

from mcp_manager.oauth_config import oauth_url
from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("value", [" https://auth.test", "https://auth.test/\npath", "https://auth.test/#fragment",
                                   "https://user:password@auth.test", "http://auth.test", "https://auth.test:invalid"])
def test_invalid_oauth_endpoint_is_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        oauth_url(value)


async def test_prm_trailing_slash_normalization_preserves_requested_resource(monkeypatch):
    implementation, _, resource = harness(monkeypatch, prm={
        "resource": "https://mcp.test/mcp/", "authorization_servers": ["https://auth.test/tenant"]})
    result = await implementation.discover(resource, {})
    assert result["resource"] == resource


@pytest.mark.parametrize("kind", ["redirect", "oversized", "issuer_fallback"])
async def test_metadata_failure_does_not_bypass_security(monkeypatch, kind):
    from mcp_manager import oauth_discovery

    resource, issuer = "https://mcp.test/mcp", "https://auth.test/tenant"

    def handle(request):
        path = request.url.path
        if path == "/mcp":
            return httpx.Response(401)
        if path == "/.well-known/oauth-protected-resource/mcp":
            if kind == "redirect":
                return httpx.Response(302, headers={"Location": "https://other.test/meta"})
            if kind == "oversized":
                return httpx.Response(200, content=b" " * 262145)
            return httpx.Response(200, json={"resource": resource, "authorization_servers": [issuer]})
        return httpx.Response(404)

    monkeypatch.setattr(oauth_discovery, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    auth = {"authorization_url": issuer + "/a", "token_url": issuer + "/t"}
    if kind == "issuer_fallback":
        context = await oauth_discovery.discover(resource, auth)
        assert context["issuer_url"] == issuer
    else:
        with pytest.raises(GatewayError):
            await oauth_discovery.discover(resource, auth)


async def test_concurrent_starts_reuse_one_registration(db_app, monkeypatch):
    calls = discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    responses = await asyncio.gather(*[
        db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start") for _ in range(3)])
    assert all(response.status_code == 200 for response in responses)
    assert len(calls) == 1


async def test_cimd_document_is_accessible_without_session(db_app):
    db_app.app.state.config.public_url = "https://gateway.example.com"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(db_app.app), base_url="http://test") as anonymous:
        response = await anonymous.get("/oauth/client-metadata.json")
    assert response.status_code == 200
    assert response.json()["client_id"] == "https://gateway.example.com/oauth/client-metadata.json"


@pytest.mark.parametrize("issuer", [None, "https://wrong.test"])
async def test_callback_checks_required_issuer_before_token_exchange(db_app, monkeypatch, issuer):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    started = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
    calls = []

    async def exchange(auth, data):
        calls.append(data)
        return {"access_token": "forbidden"}

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    params = {"state": state, "code": "code"}
    if issuer:
        params["iss"] = issuer
    response = await db_app.web.get("/api/v1/oauth/callback", params=params)
    assert response.status_code in {400, 403}
    assert not calls


async def test_issuer_change_revokes_every_shared_config_token_and_runtime_version(db_app, monkeypatch):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    endpoint = f"/api/v1/mcps/{row.id}/oauth/start"
    assert (await db_app.web.post(endpoint)).status_code == 200
    owner = db_app.actor["id"]
    async with db_app.db.locked() as session:
        for user in (owner, "other-owner"):
            await db_app.app.state.credentials.save_oauth_token(session, row, user, {
                "access_token": "old-" + user, "expires_at": None})
            db_app.app.state.runtime.credential_versions[(row.id, user)] = "previous"
    from mcp_manager import oauth_discovery
    original = oauth_discovery.discover

    async def changed(resource, auth):
        return dict(await original(resource, auth), issuer_url="https://new-issuer.test")

    monkeypatch.setattr(oauth_discovery, "discover", changed)
    assert (await db_app.web.post(endpoint)).status_code == 200
    for user in (owner, "other-owner"):
        payload = await db_app.app.state.credentials.load(row.id, user)
        assert not payload.get("oauth_token")
        assert payload["oauth_state"]["reason"] == "issuer_changed"
        assert db_app.app.state.runtime.credential_versions[(row.id, user)] is None
        assert db_app.app.state.catalog.cached(row, user)["cache_error_code"] == "auth_required"
