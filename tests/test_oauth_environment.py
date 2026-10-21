"""Imported resource URL references bind to their actual resolved destination."""
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from test_oauth_reauth import save_token

from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("style,reference", [("claude", "${MMG_OAUTH_RESOURCE}"), ("dsh", "$MMG_OAUTH_RESOURCE")])
async def test_expanded_resource_is_bound_during_authorization_and_use(db_app, monkeypatch, style, reference):
    monkeypatch.setenv("MMG_OAUTH_RESOURCE", "https://mcp.test/mcp")
    user = db_app.actor["id"]
    row = await db_app.app.state.catalog.create({
        "name": "Imported URL", "transport": "streamable-http", "mode": "disabled", "isolation": "user",
        "config": {"url": reference, "environment_expansion": style, "auth": {
            "type": "oauth", "auth_mode": "manual", "client_id": "public",
            "authorization_url": "https://auth.test/a", "token_url": "https://auth.test/t"}},
    }, user_id=user)
    endpoint = f"/api/v1/mcps/{row.id}/oauth/start"
    started = await db_app.web.post(endpoint)
    assert started.status_code == 200, started.text
    params = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    assert params["resource"] == ["https://mcp.test/mcp"]
    calls = []

    async def exchange(auth, data):
        calls.append(data)
        return {"access_token": "new-token", "expires_at": None}

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    monkeypatch.setenv("MMG_OAUTH_RESOURCE", "https://other.test/mcp")
    rejected = await db_app.web.get("/api/v1/oauth/callback", params={"state": params["state"][0], "code": "code"})
    assert rejected.status_code == 409 and not calls
    monkeypatch.setenv("MMG_OAUTH_RESOURCE", "https://mcp.test/mcp")
    await save_token(db_app, row, user, {"access_token": "token", "expires_at": time.time() + 3600})
    monkeypatch.setenv("MMG_OAUTH_RESOURCE", "https://other.test/mcp")
    with pytest.raises(GatewayError) as error:
        await db_app.app.state.oauth.credentials(row, user)
    assert error.value.code == "auth_required"
    payload = await db_app.app.state.credentials.load(row.id, user)
    assert payload["oauth_state"]["reason"] == "binding_changed"


async def test_environment_change_after_token_validation_cannot_redirect_bearer(db_app, monkeypatch):
    monkeypatch.setenv("MMG_OAUTH_RESOURCE", "https://mcp.test/mcp")
    user = db_app.actor["id"]
    row = await db_app.app.state.catalog.create({
        "name": "Queued URL", "transport": "streamable-http", "isolation": "user",
        "config": {"url": "${MMG_OAUTH_RESOURCE}", "environment_expansion": "claude", "auth": {
            "type": "oauth", "auth_mode": "manual", "client_id": "public",
            "authorization_url": "https://auth.test/a", "token_url": "https://auth.test/t"}},
    }, user_id=user)
    started = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert started.status_code == 200
    await save_token(db_app, row, user, {"access_token": "token", "expires_at": time.time() + 3600})
    original = db_app.app.state.oauth.credentials

    async def validate_then_change(*args, **kwargs):
        result = await original(*args, **kwargs)
        monkeypatch.setenv("MMG_OAUTH_RESOURCE", "https://other.test/mcp")
        return result

    monkeypatch.setattr(db_app.app.state.oauth, "credentials", validate_then_change)
    with pytest.raises(GatewayError) as error:
        await db_app.app.state.catalog.spec(row, user)
    assert error.value.code == "credential_changed"
