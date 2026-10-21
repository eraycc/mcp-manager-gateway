"""Explicit authorization chooses pre-registration, CIMD, then opted-in DCR."""
from urllib.parse import parse_qs, urlsplit


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


async def test_cimd_is_preferred_and_public_metadata_has_no_credentials(db_app, monkeypatch):
    calls = discovery(monkeypatch, cimd=True)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app)
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert response.status_code == 200, response.text
    params = parse_qs(urlsplit(response.json()["authorization_url"]).query)
    assert params["client_id"] == ["https://gateway.example.com/oauth/client-metadata.json"]
    assert params["resource"] == ["https://mcp.test/mcp"] and params["code_challenge_method"] == ["S256"]
    assert calls == []
    document = await db_app.web.get("/oauth/client-metadata.json")
    assert document.status_code == 200
    assert document.json()["client_id"] == params["client_id"][0]
    assert document.headers["cache-control"].startswith("public")
    assert "client_secret" not in document.text and "access_token" not in document.text

    async def exchange(auth, data):
        assert data["resource"] == "https://mcp.test/mcp"
        return {"access_token": "cimd-access", "expires_at": None}
    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    callback = await db_app.web.get("/api/v1/oauth/callback", params={
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


async def test_cimd_support_without_public_https_does_not_silently_use_dcr(db_app, monkeypatch):
    calls = discovery(monkeypatch, cimd=True)
    row = await server(db_app)
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert response.status_code != 200 and "PUBLIC_URL" in response.text
    assert calls == []


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
