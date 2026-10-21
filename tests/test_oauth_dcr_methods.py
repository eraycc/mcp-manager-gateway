"""DCR negotiates client authentication and keeps generated secrets out of editable config."""
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_oauth_registration import server

from mcp_manager import oauth_discovery
from mcp_manager.runtime import GatewayError
from mcp_manager.transports import validate_config


@pytest.mark.parametrize("method", ["none", "client_secret_basic", "client_secret_post"])
def test_automatic_configuration_accepts_a_token_method_without_manual_secret(method):
    validate_config("streamable-http", {"url": "https://mcp.test/mcp", "auth": {
        "type": "oauth", "auth_mode": "auto", "allow_dcr": True, "client_name": "Codex",
        "token_endpoint_auth_method": method}})


def metadata(*, methods=None, figma=False):
    issuer = "https://api.figma.com" if figma else "https://auth.test"
    return {"issuer_url": issuer, "resource": "https://mcp.figma.com/mcp" if figma else "https://mcp.test/mcp",
            "authorization_url": "https://www.figma.com/oauth/mcp" if figma else issuer + "/a",
            "token_url": issuer + ("/v1/oauth/token" if figma else "/t"),
            "registration_url": issuer + ("/v1/oauth/mcp/register" if figma else "/register"),
            "token_endpoint_auth_methods_supported": methods or ["client_secret_basic", "client_secret_post"],
            "token_endpoint_auth_method": "client_secret_post"}


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
async def test_registration_requests_and_preserves_confidential_client(monkeypatch, method):
    context = metadata()
    context["token_endpoint_auth_method"] = method
    requests = []

    def handle(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["token_endpoint_auth_method"] == method
        assert "refresh_token" in body["grant_types"]
        assert "client_secret" not in body
        return httpx.Response(201, json={"client_id": "dynamic-client", "client_secret": "generated-secret",
            "token_endpoint_auth_method": method, "client_secret_expires_at": 0})

    monkeypatch.setattr(oauth_discovery, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    result = await oauth_discovery.register(context, "Codex", "http://localhost:8765/api/v1/oauth/callback")
    assert result["client_id"] == "dynamic-client" and result["client_secret"] == "generated-secret"
    assert result["token_endpoint_auth_method"] == method and len(requests) == 1


@pytest.mark.parametrize("figma", [True, False])
async def test_none_response_with_secret_is_compatible_only_for_verified_figma(monkeypatch, figma):
    context = metadata(figma=figma)
    monkeypatch.setattr(oauth_discovery, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(201, json={"client_id": "client", "client_secret": "secret",
                                                "token_endpoint_auth_method": "none"}))))
    if figma:
        result = await oauth_discovery.register(context, "Codex", "http://localhost/callback")
        assert result["token_endpoint_auth_method"] == "client_secret_post"
    else:
        with pytest.raises(GatewayError):
            await oauth_discovery.register(context, "Codex", "http://localhost/callback")


@pytest.mark.parametrize("secret,expiry", [(None, 0), ("", 0), ([], 0), ("secret", 1), ("secret", True)])
async def test_invalid_or_expired_registration_secret_fails_before_authorization(monkeypatch, secret, expiry):
    monkeypatch.setattr(oauth_discovery, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(201, json={"client_id": "client", "client_secret": secret,
            "client_secret_expires_at": expiry, "token_endpoint_auth_method": "client_secret_post"}))))
    with pytest.raises(GatewayError):
        await oauth_discovery.register(metadata(), "Codex", "http://localhost/callback")


@pytest.mark.parametrize("method", [None, "client_secret_basic", "client_secret_post"])
async def test_dcr_secret_survives_cache_callback_and_refresh_without_becoming_editable(db_app, monkeypatch, method):
    chosen = method or "client_secret_post"
    auth = {"type": "oauth", "auth_mode": "auto", "allow_dcr": True, "client_name": "Codex"}
    if method:
        auth["token_endpoint_auth_method"] = method
    row = await server(db_app, auth)
    registrations = []

    async def discover(resource, auth):
        return metadata() | {"resource": resource, "cimd_supported": False, "scopes": []}

    def register(request):
        registrations.append(request)
        assert json.loads(request.content)["token_endpoint_auth_method"] == chosen
        return httpx.Response(201, json={"client_id": "registered", "client_secret": "generated-secret",
                                       "token_endpoint_auth_method": chosen, "client_secret_expires_at": 0})

    monkeypatch.setattr(oauth_discovery, "discover", discover)
    monkeypatch.setattr(oauth_discovery, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(register)))
    endpoint = f"/api/v1/mcps/{row.id}/oauth/start"
    for _ in range(2):
        response = await db_app.web.post(endpoint)
        assert response.status_code == 200, response.text
    assert len(registrations) == 1
    owner = db_app.actor["id"]
    payload = await db_app.app.state.credentials.load(row.id, "service")
    assert payload["oauth_registration"]["auth"]["client_secret"] == "generated-secret"
    from mcp_manager.database import SystemSetting
    async with db_app.db.session() as session:
        sealed = await session.get(SystemSetting, db_app.app.state.credentials.key(row.id, "service"))
        assert "generated-secret" not in sealed.value
    assert "client_secret" not in payload["auth_config"]
    editable = await db_app.app.state.credentials.materialize(row, owner)
    assert "client_secret" not in editable["auth"]
    assert "generated-secret" not in response.text

    requests = []

    def exchange(request):
        body = parse_qs(request.content.decode())
        requests.append(body)
        if chosen == "client_secret_post":
            assert body["client_secret"] == ["generated-secret"] and "Authorization" not in request.headers
        else:
            assert "client_secret" not in body and "client_id" not in body
            assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "refresh", "expires_in": 3600})

    from mcp_manager import oauth
    monkeypatch.setattr(oauth, "token_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(exchange)))
    state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
    callback = await db_app.web.get("/api/v1/oauth/callback", params={"state": state, "code": "code"})
    assert callback.status_code == 303, callback.text
    async with db_app.db.locked() as session:
        value = await db_app.app.state.credentials.load(row.id, owner, session=session)
        value["oauth_token"]["expires_at"] = time.time() - 1
        await db_app.app.state.credentials.write(session, row.id, owner, value)
    assert (await db_app.app.state.oauth.credentials(row, owner))["access_token"] == "new-access"
    assert [body["grant_type"][0] for body in requests] == ["authorization_code", "refresh_token"]


@pytest.mark.parametrize("supported,expected", [
    (None, "none"), (["none", "client_secret_post"], "none"),
    (["client_secret_post", "client_secret_basic"], "client_secret_post"),
    (["client_secret_basic"], "client_secret_basic"),
])
def test_automatic_dcr_negotiates_from_server_capabilities(supported, expected):
    context = {} if supported is None else {"token_endpoint_auth_methods_supported": supported}
    assert oauth_discovery.registration_method({}, context) == expected


@pytest.mark.parametrize("supported,method", [([], None), (["private_key_jwt"], None),
                                             (["client_secret_post"], "none"),
                                             (["none"], "client_secret_basic")])
def test_unsupported_registration_method_is_rejected_before_dcr(supported, method):
    with pytest.raises(GatewayError) as error:
        oauth_discovery.registration_method({"token_endpoint_auth_method": method},
            {"token_endpoint_auth_methods_supported": supported})
    assert error.value.code == "oauth_configuration_required"


async def test_cimd_rejects_explicit_secret_method_without_falling_back_to_dcr(db_app, monkeypatch):
    from test_oauth_registration import discovery

    calls = discovery(monkeypatch, cimd=True)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    row = await server(db_app, {"type": "oauth", "auth_mode": "cimd_only",
        "token_endpoint_auth_method": "client_secret_post"})
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert response.status_code == 502 and "CIMD" in response.text and "none" in response.text
    assert not calls


@pytest.mark.parametrize("returned", ["none", "private_key_jwt", {}, None])
async def test_explicit_method_does_not_accept_server_downgrade_or_unknown_method(monkeypatch, returned):
    context = metadata(methods=["none", "client_secret_post"]) | {"token_auth_method_explicit": True}
    monkeypatch.setattr(oauth_discovery, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(201, json={"client_id": "client", "client_secret": "secret",
                                                "token_endpoint_auth_method": returned}))))
    with pytest.raises(GatewayError) as error:
        await oauth_discovery.register(context, "Codex", "http://localhost/callback")
    assert error.value.code == "oauth_registration_failed"


async def test_changing_token_method_invalidates_registration_and_pending_callback(db_app, monkeypatch):
    from test_oauth_registration import discovery

    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    started = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
    config = await db_app.app.state.credentials.materialize(row, db_app.actor["id"])
    config["auth"]["token_endpoint_auth_method"] = "client_secret_post"
    row = await db_app.app.state.catalog.update(row.id, {"config": config}, user_id=db_app.actor["id"])
    payload = await db_app.app.state.credentials.load(row.id, "service")
    assert not payload.get("oauth_registration")
    callback = await db_app.web.get("/api/v1/oauth/callback", params={
        "state": state, "code": "code", "iss": "https://auth.test"})
    assert callback.status_code in {403, 409}


async def test_expired_dcr_secret_requires_explicit_start_to_register_again(db_app, monkeypatch):
    row = await server(db_app, {"type": "oauth", "auth_mode": "auto", "allow_dcr": True,
                               "client_name": "Codex", "token_endpoint_auth_method": "client_secret_post"})
    requests = []

    async def discover(resource, auth):
        return metadata() | {"resource": resource, "cimd_supported": False}

    def register(request):
        requests.append(request)
        return httpx.Response(201, json={"client_id": "registered-" + str(len(requests)), "client_secret": "secret",
                                       "token_endpoint_auth_method": "client_secret_post", "client_secret_expires_at": 0})

    monkeypatch.setattr(oauth_discovery, "discover", discover)
    monkeypatch.setattr(oauth_discovery, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(register)))
    endpoint = f"/api/v1/mcps/{row.id}/oauth/start"
    assert (await db_app.web.post(endpoint)).status_code == 200
    owner = db_app.actor["id"]
    async with db_app.db.locked() as session:
        payload = await db_app.app.state.credentials.load(row.id, "service", session=session)
        payload["oauth_registration"]["auth"]["client_secret_expires_at"] = 1
        await db_app.app.state.credentials.write(session, row.id, "service", payload)
        for identity in (owner, "other-owner"):
            await db_app.app.state.credentials.save_oauth_token(session, row, identity,
                {"access_token": "valid-access", "expires_at": time.time() + 3600})

    async def forbidden_exchange(*args):
        pytest.fail("An expired client secret must not be sent to the token endpoint")

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", forbidden_exchange)
    for _ in range(2):
        with pytest.raises(GatewayError) as error:
            await db_app.app.state.oauth.credentials(row, owner)
        assert error.value.code == "auth_required"
    assert len(requests) == 1
    assert (await db_app.web.post(endpoint)).status_code == 200
    assert len(requests) == 2
    for identity in (owner, "other-owner"):
        payload = await db_app.app.state.credentials.load(row.id, identity)
        assert not payload.get("oauth_token")
        assert payload["oauth_state"]["reason"] == "client_changed"


async def test_personal_dcr_registrations_and_generated_secrets_are_isolated(db_app, monkeypatch):
    auth = {"type": "oauth", "auth_mode": "auto", "allow_dcr": True, "client_name": "Codex",
            "config_isolation": "user", "token_endpoint_auth_method": "client_secret_post"}
    row = await server(db_app, auth)
    requests = []

    async def discover(resource, config):
        return metadata() | {"resource": resource, "cimd_supported": False}

    def register(request):
        requests.append(request)
        return httpx.Response(201, json={"client_id": "client-" + str(len(requests)),
            "client_secret": "secret-" + str(len(requests)), "token_endpoint_auth_method": "client_secret_post"})

    monkeypatch.setattr(oauth_discovery, "discover", discover)
    monkeypatch.setattr(oauth_discovery, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(register)))
    endpoint = f"/api/v1/mcps/{row.id}/oauth/start"
    first = await db_app.web.post(endpoint)
    assert first.status_code == 200, first.text
    async with httpx.AsyncClient(transport=httpx.ASGITransport(db_app.app), base_url="http://test") as other:
        await other.post("/api/v1/auth/register", json={"username": "other-method-user", "password": "password12345"})
        login = await other.post("/api/v1/auth/login", json={"username": "other-method-user", "password": "password12345"})
        assert login.status_code == 200
        identity = login.json()["id"]
        other.headers["X-CSRF-Token"] = other.cookies["mcp_csrf"]
        from mcp_manager.database import User
        async with db_app.db.locked() as session:
            account = await session.get(User, identity)
            account.scope_mode, account.mcp_ids = "selected", [row.id]
            await db_app.app.state.credentials.write(session, row.id, identity, {
                "auth_config": {key: value for key, value in auth.items() if key != "config_isolation"}})
        second = await other.post(endpoint)
        assert second.status_code == 200, second.text
    assert len(requests) == 2
    for index, owner in enumerate((db_app.actor["id"], identity), 1):
        payload = await db_app.app.state.credentials.load(row.id, owner)
        assert payload["oauth_registration"]["auth"]["client_id"] == "client-" + str(index)
        assert payload["oauth_registration"]["auth"]["client_secret"] == "secret-" + str(index)
    assert not await db_app.app.state.credentials.load(row.id, "service")
    assert "secret-1" not in first.text and "secret-2" not in second.text
