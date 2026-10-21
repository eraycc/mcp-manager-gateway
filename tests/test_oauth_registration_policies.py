"""Four registration policies and bounded CIMD-to-DCR authorization restart."""
from urllib.parse import parse_qs, urlsplit

import pytest
from test_oauth_registration import discovery, server

from mcp_manager.oauth_config import comparable_settings
from mcp_manager.transports import validate_config


def params(response):
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["authorization_url"]).query)


@pytest.mark.parametrize("old,new", [
    ({"auth_mode": "manual", "client_id": "client"}, "pre_registration"),
    ({"auth_mode": "auto", "allow_dcr": True, "client_name": "Codex"}, "prefer_cimd_fallback_dcr"),
    ({"auth_mode": "auto", "allow_dcr": False}, "cimd_only"),
    ({"auth_mode": "dcr", "client_name": "Codex"}, "dcr_only"),
])
def test_legacy_modes_match_canonical_settings(old, new):
    current = {key: value for key, value in old.items() if key != "allow_dcr"}
    current["auth_mode"] = new
    assert comparable_settings(old) == comparable_settings(current)
    validate_config("streamable-http", {"url": "https://mcp.test/mcp", "auth": {"type": "oauth", **current}})


@pytest.mark.parametrize("mode,cimd,public,expected", [
    ("prefer_cimd_fallback_dcr", True, False, "registered-client"),
    ("prefer_cimd_fallback_dcr", False, False, "registered-client"),
    ("prefer_cimd_fallback_dcr", True, True, "https://gateway.example.com/oauth/client-metadata.json"),
    ("dcr_only", True, True, "registered-client"),
])
async def test_policy_selects_available_registration(db_app, monkeypatch, mode, cimd, public, expected):
    calls = discovery(monkeypatch, cimd=cimd)
    if public:
        db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app, {"type": "oauth", "auth_mode": mode, "client_name": "Codex"})
    result = params(await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    assert result["client_id"] == [expected]
    assert "client_name" not in result
    assert len(calls) == int(expected == "registered-client")


@pytest.mark.parametrize("public,cimd", [(False, True), (True, False)])
async def test_cimd_only_never_falls_back(db_app, monkeypatch, public, cimd):
    calls = discovery(monkeypatch, cimd=cimd)
    if public:
        db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app, {"type": "oauth", "auth_mode": "cimd_only"})
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert response.status_code == 502 and "oauth_configuration_required" in response.text
    assert not calls


@pytest.mark.parametrize("error", ["invalid_client", "invalid_client_metadata", "unauthorized_client"])
async def test_cimd_error_callback_restarts_once_with_dcr_and_fresh_pkce(db_app, monkeypatch, error):
    calls = discovery(monkeypatch, cimd=True)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app, {"type": "oauth", "auth_mode": "prefer_cimd_fallback_dcr", "client_name": "Custom"})
    first = params(await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    rejected = await db_app.web.get("/api/v1/oauth/callback", params={
        "state": first["state"][0], "error": error, "iss": "https://auth.test"})
    assert rejected.status_code == 303, rejected.text
    second = parse_qs(urlsplit(rejected.headers["location"]).query)
    assert second["client_id"] == ["registered-client"]
    assert second["state"] != first["state"] and second["code_challenge"] != first["code_challenge"]
    assert calls == [("Custom", "https://gateway.example.com/api/v1/oauth/callback")]
    again = await db_app.web.get("/api/v1/oauth/callback", params={
        "state": second["state"][0], "error": error, "iss": "https://auth.test"})
    assert again.status_code == 400 and len(calls) == 1
    replay = await db_app.web.get("/api/v1/oauth/callback", params={
        "state": first["state"][0], "error": error, "iss": "https://auth.test"})
    assert replay.status_code == 403


@pytest.mark.parametrize("error,issuer,status", [
    ("access_denied", "https://auth.test", 400),
    ("server_error", "https://auth.test", 400),
    ("invalid_request", "https://auth.test", 400),
    ("invalid_client", "https://evil.test", 403),
    ("invalid_client", "", 403),
])
async def test_unrelated_or_untrusted_errors_never_trigger_dcr(db_app, monkeypatch, error, issuer, status):
    calls = discovery(monkeypatch, cimd=True)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app, {"type": "oauth", "auth_mode": "prefer_cimd_fallback_dcr", "client_name": "Codex"})
    first = params(await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    response = await db_app.web.get("/api/v1/oauth/callback", params={
        "state": first["state"][0], "error": error, "iss": issuer})
    assert response.status_code == status and not calls


async def test_cimd_only_callback_error_does_not_register(db_app, monkeypatch):
    calls = discovery(monkeypatch, cimd=True)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app, {"type": "oauth", "auth_mode": "cimd_only"})
    first = params(await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    response = await db_app.web.get("/api/v1/oauth/callback", params={
        "state": first["state"][0], "error": "invalid_client", "iss": "https://auth.test"})
    assert response.status_code == 400 and not calls


@pytest.mark.parametrize("mode", ["prefer_cimd_fallback_dcr", "dcr_only"])
async def test_missing_registration_endpoint_has_actionable_error(db_app, monkeypatch, mode):
    from mcp_manager import oauth_discovery

    calls = discovery(monkeypatch, cimd=False)
    original = oauth_discovery.discover

    async def discover_without_dcr(resource, auth):
        context = await original(resource, auth)
        context.pop("registration_url")
        return context

    monkeypatch.setattr(oauth_discovery, "discover", discover_without_dcr)
    row = await server(db_app, {"type": "oauth", "auth_mode": mode})
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert response.status_code == 502 and "预注册" in response.text and "Client ID" in response.text
    assert not calls


@pytest.mark.parametrize("error,restarts", [("invalid_client", True), ("invalid_client_metadata", True),
    ("invalid_grant", False), ("access_denied", False), ("server_error", False), ("timeout", False)])
async def test_token_client_rejection_starts_new_flow_without_replaying_code(db_app, monkeypatch, error, restarts):
    import httpx

    from mcp_manager import oauth

    calls = discovery(monkeypatch, cimd=True)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app, {"type": "oauth", "auth_mode": "prefer_cimd_fallback_dcr"})
    first = params(await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    exchanges = []

    def exchange(request):
        exchanges.append(parse_qs(request.content.decode()))
        if error == "timeout":
            raise httpx.ReadTimeout("uncertain result")
        return httpx.Response(400, json={"error": error, "error_description": "secret-do-not-echo"})

    monkeypatch.setattr(oauth, "token_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(exchange)))
    response = await db_app.web.get("/api/v1/oauth/callback", params={
        "state": first["state"][0], "code": "single-use-code", "iss": "https://auth.test"})
    assert response.status_code == (303 if restarts else 502), response.text
    assert len(exchanges) == 1 and exchanges[0]["code"] == ["single-use-code"]
    assert "secret-do-not-echo" not in response.text
    assert len(calls) == int(restarts)
    if restarts:
        second = parse_qs(urlsplit(response.headers["location"]).query)
        assert second["client_id"] == ["registered-client"] and second["state"] != first["state"]


@pytest.mark.parametrize("mutation", ["disconnect", "config", "expired"])
async def test_invalidated_cimd_flow_cannot_trigger_dcr(db_app, monkeypatch, mutation):
    calls = discovery(monkeypatch, cimd=True)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app, {"type": "oauth", "auth_mode": "prefer_cimd_fallback_dcr"})
    first = params(await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    if mutation == "disconnect":
        await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/disconnect")
    elif mutation == "config":
        config = await db_app.app.state.credentials.materialize(row, db_app.actor["id"])
        config["auth"]["auth_mode"] = "cimd_only"
        config["auth"].pop("client_name", None)
        await db_app.app.state.catalog.update(row.id, {"config": config}, user_id=db_app.actor["id"])
    else:
        db_app.app.state.oauth.pending[first["state"][0]]["expires"] = 1
    response = await db_app.web.get("/api/v1/oauth/callback", params={
        "state": first["state"][0], "error": "invalid_client", "iss": "https://auth.test"})
    assert response.status_code in {403, 409} and not calls


@pytest.mark.parametrize("old_mode,new_mode", [("auto", "prefer_cimd_fallback_dcr"), ("dcr", "dcr_only")])
async def test_legacy_policy_is_canonical_on_save_without_losing_registered_token(db_app, monkeypatch, old_mode, new_mode):
    import time

    from mcp_manager.oauth_config import registration_fingerprint

    calls = discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    started = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    params(started)
    owner = db_app.actor["id"]
    async with db_app.db.locked() as session:
        config_payload = await db_app.app.state.credentials.load(row.id, "service", session=session)
        legacy = {"type": "oauth", "auth_mode": "auto", "allow_dcr": True, "client_name": "Codex"}
        config_payload["auth_config"] = legacy if old_mode == "auto" else {key: value for key, value in legacy.items() if key != "allow_dcr"} | {"auth_mode": "dcr"}
        assert config_payload["oauth_registration"]["config_fingerprint"] == registration_fingerprint(
            legacy, "https://mcp.test/mcp", db_app.app.state.config.public_url + "/api/v1/oauth/callback")
        await db_app.app.state.credentials.write(session, row.id, "service", config_payload)
        await db_app.app.state.credentials.save_oauth_token(session, row, owner,
            {"access_token": "legacy-token", "expires_at": time.time() + 3600})
    config = await db_app.app.state.credentials.materialize(row, owner)
    config["auth"]["auth_mode"] = new_mode
    config["auth"].pop("allow_dcr", None)
    row = await db_app.app.state.catalog.update(row.id, {"config": config}, user_id=owner)
    payload = await db_app.app.state.credentials.load(row.id, "service")
    assert payload["auth_config"]["auth_mode"] == new_mode
    assert "allow_dcr" not in payload["auth_config"] and payload.get("oauth_registration")
    assert (await db_app.app.state.oauth.credentials(row, owner))["access_token"] == "legacy-token"
    params(await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    assert len(calls) == 1


@pytest.mark.parametrize("callback_path", ["/api/v1/oauth/callback", "/callback"])
async def test_cimd_fallback_completes_dcr_authorization_and_reports_sanitized_reason(db_app, monkeypatch, callback_path):
    calls = discovery(monkeypatch, cimd=True)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app, {"type": "oauth", "auth_mode": "prefer_cimd_fallback_dcr",
                               "client_name": "Custom", "callback_path": callback_path})
    audits = []

    async def audit(action, user_id, server_id, **details):
        audits.append((action, details))

    monkeypatch.setattr(db_app.app.state.oauth, "audit", audit)
    first = params(await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start", json={"return_to": "profile"}))
    rejected = await db_app.web.get(callback_path, params={"state": first["state"][0],
        "error": "invalid_client_metadata", "error_description": "private-error-details", "iss": "https://auth.test"})
    assert rejected.status_code == 303, rejected.text
    second = parse_qs(urlsplit(rejected.headers["location"]).query)
    assert second["redirect_uri"] == first["redirect_uri"]
    assert calls == [("Custom", "https://gateway.example.com" + callback_path)]
    assert ("oauth.cimd_fallback", {"reason": "invalid_client_metadata", "error_code": "invalid_client_metadata",
        "stage": "authorization", "result": "dcr"}) in audits
    assert "private-error-details" not in repr(audits)

    async def exchange(auth, data):
        assert auth["client_id"] == "registered-client"
        assert data["code"] == "fresh-code" and data["redirect_uri"] == second["redirect_uri"][0]
        return {"access_token": "fresh-access", "expires_at": None}

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    completed = await db_app.web.get(callback_path, params={
        "state": second["state"][0], "code": "fresh-code", "iss": "https://auth.test"})
    assert completed.status_code == 303 and completed.headers["location"].endswith("/#/profile")
    status = (await db_app.web.get(f"/api/v1/mcps/{row.id}/oauth/status")).json()
    assert status["authorized"] and status["registration_mode"] == "dcr"


async def test_pre_registration_requires_id_and_never_discovers_or_registers_with_explicit_endpoints(db_app, monkeypatch):
    from mcp_manager import oauth_discovery

    with pytest.raises(ValueError, match="Client ID"):
        validate_config("streamable-http", {"url": "https://mcp.test/mcp",
            "auth": {"type": "oauth", "auth_mode": "pre_registration"}})

    async def forbidden(*args):
        pytest.fail("Pre-registration with explicit endpoints must skip discovery and registration")

    monkeypatch.setattr(oauth_discovery, "discover", forbidden)
    monkeypatch.setattr(oauth_discovery, "register", forbidden)
    row = await server(db_app, {"type": "oauth", "auth_mode": "pre_registration", "client_id": "manual",
        "authorization_url": "https://auth.test/a", "token_url": "https://auth.test/t"})
    result = params(await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    assert result["client_id"] == ["manual"] and "client_name" not in result


@pytest.mark.parametrize("explicit", [False, True])
async def test_preferred_policy_falls_back_for_cimd_incompatible_token_method(db_app, monkeypatch, explicit):
    from mcp_manager import oauth_discovery

    db_app.app.state.config.public_url = "https://gateway.example.com"
    calls = discovery(monkeypatch, cimd=True)
    original = oauth_discovery.discover

    async def discover_secret_only(resource, auth):
        return await original(resource, auth) | {"token_endpoint_auth_methods_supported": ["client_secret_post"]}

    async def register(context, name, redirect):
        calls.append((name, redirect))
        assert context["token_endpoint_auth_method"] == "client_secret_post"
        return {"client_id": "secret-client", "client_secret": "sealed-only",
                "token_endpoint_auth_method": "client_secret_post"}

    monkeypatch.setattr(oauth_discovery, "discover", discover_secret_only)
    monkeypatch.setattr(oauth_discovery, "register", register)
    auth = {"type": "oauth", "auth_mode": "prefer_cimd_fallback_dcr"}
    if explicit:
        auth["token_endpoint_auth_method"] = "client_secret_post"
    row = await server(db_app, auth)
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert params(response)["client_id"] == ["secret-client"] and len(calls) == 1
    assert "sealed-only" not in response.text
    config = await db_app.app.state.credentials.materialize(row, db_app.actor["id"])
    assert "client_secret" not in config["auth"]
