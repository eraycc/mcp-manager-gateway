"""Explicit authorization chooses pre-registration, CIMD, then opted-in DCR."""
from urllib.parse import parse_qs, urlsplit

import pytest


async def server(h, auth=None):
    return await h.app.state.catalog.create({
        "name": "Registration", "transport": "streamable-http", "mode": "disabled", "isolation": "user",
        "config": {"url": "https://mcp.test/mcp", "auth": auth or {
            "type": "oauth", "auth_mode": "auto", "allow_dcr": True, "client_name": "Codex"}},
    }, user_id=h.actor["id"])


def discovery(monkeypatch, *, cimd):
    from mcp_manager import oauth_discovery
    calls = []
    async def discover(resource, auth):
        return {"issuer_url": "https://auth.test", "authorization_url": "https://auth.test/a",
                "token_url": "https://auth.test/t", "registration_url": "https://auth.test/register",
                "resource": resource, "cimd_supported": cimd, "scopes": ["read"], "iss_required": True}
    async def register(context, name, redirect):
        calls.append((name, redirect))
        return {"client_id": "registered-client", "token_endpoint_auth_method": context.get("token_endpoint_auth_method", "none")}
    monkeypatch.setattr(oauth_discovery, "discover", discover)
    monkeypatch.setattr(oauth_discovery, "register", register)
    return calls


@pytest.mark.parametrize("callback_path", ["/api/v1/oauth/callback", "/callback"])
async def test_cimd_is_preferred_and_public_metadata_has_no_credentials(db_app, monkeypatch, callback_path):
    calls = discovery(monkeypatch, cimd=True)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app, {"type": "oauth", "auth_mode": "auto", "allow_dcr": True,
                               "client_name": "Codex", "callback_path": callback_path})
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert response.status_code == 200, response.text
    params = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    assert params["client_id"] == ["https://gateway.example.com/oauth/client-metadata.json"]
    assert params["resource"] == ["https://mcp.test/mcp"] and params["code_challenge_method"] == ["S256"]
    assert calls == []
    document = await db_app.web.get("/oauth/client-metadata.json")
    assert document.status_code == 200
    assert document.json()["grant_types"] == ["authorization_code", "refresh_token"]
    assert document.json()["application_type"] == "web"
    assert document.json()["client_id"] == params["client_id"][0]
    assert params["redirect_uri"] == ["https://gateway.example.com" + callback_path]
    assert params["redirect_uri"][0] in document.json()["redirect_uris"]
    assert document.headers["cache-control"].startswith("public")
    assert "client_secret" not in document.text and "access_token" not in document.text

    async def exchange(auth, data):
        assert data["resource"] == "https://mcp.test/mcp"
        assert data["redirect_uri"] == "https://gateway.example.com" + callback_path
        return {"access_token": "cimd-access", "expires_at": None}
    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    callback = await db_app.web.get(callback_path, params={
        "state": params["state"][0], "code": "code", "iss": "https://auth.test"})
    assert callback.status_code == 303, callback.text
    status = (await db_app.web.get(f"/api/v1/mcps/{row.id}/oauth/status")).json()
    assert status["authorized"] and status["registration_mode"] == "cimd"


async def test_dcr_is_opt_in_and_registration_is_reused(db_app, monkeypatch):
    calls = discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    scope = None
    for _ in range(2):
        response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
        assert response.status_code == 200, response.text
        params = parse_qs(urlsplit(response.json()["authorization_url"]).query)
        assert params["client_id"] == ["registered-client"]
        current_scope = await db_app.app.state.catalog.index_scope(row, db_app.actor["id"])
        if scope is not None:
            assert current_scope == scope, "Reusing registration must not invalidate every shared-config catalog"
        scope = current_scope
    assert len(calls) == 1 and calls[0][0] == "Codex"
    payload = await db_app.app.state.credentials.load(row.id, "service")
    assert payload["oauth_registration"]["auth"]["client_id"] == "registered-client"


async def test_cimd_support_without_public_https_falls_back_when_allowed(db_app, monkeypatch):
    calls = discovery(monkeypatch, cimd=True)
    row = await server(db_app)
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert response.status_code == 200, response.text
    assert parse_qs(urlsplit(response.json()["authorization_url"]).query)["client_id"] == ["registered-client"]
    assert len(calls) == 1


async def test_no_dcr_without_opt_in_and_manual_public_client_skips_registration(db_app, monkeypatch):
    calls = discovery(monkeypatch, cimd=False)
    row = await server(db_app, {"type": "oauth", "auth_mode": "auto", "allow_dcr": False})
    assert (await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")).status_code != 200
    manual = await server(db_app, {"type": "oauth", "auth_mode": "manual", "client_id": "pre-registered",
        "authorization_url": "https://auth.test/a", "token_url": "https://auth.test/t"})
    response = await db_app.web.post(f"/api/v1/mcps/{manual.id}/oauth/start")
    assert response.status_code == 200, response.text
    assert parse_qs(urlsplit(response.json()["authorization_url"]).query)["client_id"] == ["pre-registered"]
    assert calls == []


async def test_disconnect_invalidates_pending_authorization(db_app, monkeypatch):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert response.status_code == 200, response.text
    state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
    await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/disconnect")
    callback = await db_app.web.get("/api/v1/oauth/callback", params={"state": state, "code": "code", "iss": "https://auth.test"})
    assert callback.status_code in {403, 409}


@pytest.mark.parametrize("mode,supported,scopes,want", [
    ("cimd_only", ["read", "offline_access"], ["read"], "read offline_access"),
    ("dcr_only", ["read", "offline_access"], ["read"], "read offline_access"),
    ("pre_registration", ["read", "offline_access"], ["read"], "read offline_access"),
    ("cimd_only", ["read"], ["read"], "read"),
    ("cimd_only", None, ["read"], "read"),
    ("cimd_only", [], ["read"], "read"),
    ("cimd_only", ["offline_access"], ["read", "offline_access"], "read offline_access"),
    ("cimd_only", ["offline_access"], "read offline_access", "read offline_access"),
    ("cimd_only", ["offline_access"], [], "read offline_access"),
    ("cimd_only", "offline_access", ["read"], "read"),
])
async def test_authorization_adds_offline_scope_only_when_as_advertises_it(
    db_app, monkeypatch, mode, supported, scopes, want,
):
    from test_oauth_discovery import harness

    metadata = {"issuer": "https://auth.test/tenant",
        "authorization_endpoint": "https://auth.test/tenant/authorize",
        "token_endpoint": "https://auth.test/tenant/token",
        "registration_endpoint": "https://auth.test/tenant/register",
        "client_id_metadata_document_supported": True}
    if supported is not None:
        metadata["scopes_supported"] = supported
    harness(monkeypatch, metadata=metadata)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    auth = {"type": "oauth", "auth_mode": mode, "scopes": scopes, "client_name": "Codex"}
    if mode == "pre_registration":
        auth.pop("client_name")
        auth["client_id"] = "pre-registered"
    row = await server(db_app, auth)
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert response.status_code == 200, response.text
    query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    assert query["scope"] == [want]
    configured = await db_app.app.state.credentials.materialize(row, db_app.actor["id"])
    assert configured["auth"]["scopes"] == scopes
    registration = await db_app.app.state.credentials.load(row.id, "service")
    assert registration["oauth_registration"]["auth"]["scopes"] == (
        scopes.split() if isinstance(scopes, str) else scopes or ["read"])
