"""External CIMD keeps MMG private and verifies the actual callback and public file."""
import copy
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_oauth_registration import discovery, server

URL = "https://metadata.example.com/mmg.json"
REDIRECT = "http://127.0.0.1:8765/api/v1/oauth/callback"


def document(**changes):
    return {"client_id": URL, "client_name": "Private MMG", "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", **changes}


def remote(monkeypatch, value=None, status=200, headers=None, raw=None):
    from mcp_manager import oauth_metadata
    requests = []

    async def handle(request):
        requests.append(request)
        assert request.extensions["oauth_public_only"] is True
        return httpx.Response(status, headers=headers or {"Content-Type": "application/json"},
                              content=raw if raw is not None else None,
                              json=(value if value is not None else document()) if raw is None else None)

    monkeypatch.setattr(oauth_metadata, "http_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=False))
    return requests


async def verify(h, **changes):
    return await h.web.post("/api/v1/oauth/client-metadata/verify",
                           json={"source": "external", "url": URL, **changes})


async def test_external_template_works_with_loopback_public_url(db_app):
    response = await db_app.web.get("/api/v1/oauth/client-metadata/template",
        params={"source": "external", "hosting_url": URL, "callback_path": "/callback"})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["client_id"] == URL
    assert value["redirect_uris"] == ["http://127.0.0.1:8765/callback"]
    assert value["grant_types"] == ["authorization_code", "refresh_token"]
    assert value["application_type"] == "native"
    assert value["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in value


async def test_external_authorization_verifies_then_completes_with_private_mmg(db_app, monkeypatch):
    calls = discovery(monkeypatch, cimd=True)
    fetched = remote(monkeypatch)
    row = await server(db_app, {"type": "oauth", "auth_mode": "cimd_only",
        "cimd_metadata_source": "external", "cimd_external_url": URL})
    for _ in range(2):
        result = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
        assert result.status_code == 200, result.text
    params = parse_qs(urlsplit(result.json()["authorization_url"]).query)
    assert params["client_id"] == [URL] and params["redirect_uri"] == [REDIRECT]
    assert len(fetched) == 1 and not calls

    grants = []

    async def exchange(auth, data):
        assert auth["client_id"] == URL
        grants.append(data["grant_type"])
        if data["grant_type"] == "authorization_code":
            assert data["redirect_uri"] == REDIRECT
        else:
            assert data["refresh_token"] == "external-refresh"
        return {"access_token": "external-access", "refresh_token": "external-refresh", "expires_at": None}

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    completed = await db_app.web.get("/api/v1/oauth/callback", params={
        "state": params["state"][0], "code": "code", "iss": "https://auth.test"})
    assert completed.status_code == 303
    status = (await db_app.web.get(f"/api/v1/mcps/{row.id}/oauth/status")).json()
    assert status["authorized"] and status["registration_mode"] == "cimd"
    owner = db_app.actor["id"]
    async with db_app.db.locked() as session:
        payload = await db_app.app.state.credentials.load(row.id, owner, session=session)
        assert payload["oauth_token"]["refresh_token"] == "external-refresh"
        payload["oauth_token"]["expires_at"] = 0
        await db_app.app.state.credentials.write(session, row.id, owner, payload)
    assert (await db_app.app.state.oauth.credentials(row, owner))["access_token"] == "external-access"
    assert grants == ["authorization_code", "refresh_token"]


@pytest.mark.parametrize("changes,code", [
    ({"client_id": URL + "/"}, "client_id_mismatch"),
    ({"redirect_uris": ["https://other.example.com/api/v1/oauth/callback"]}, "redirect_uri_mismatch"),
    ({"redirect_uris": []}, "invalid_redirect_uris"),
    ({"redirect_uris": ["http://192.168.1.10/api/v1/oauth/callback"]}, "invalid_redirect_uris"),
    ({"client_name": ""}, "invalid_metadata"),
    ({"token_endpoint_auth_method": "client_secret_post"}, "unsupported_auth_method"),
    ({"client_secret": "must-never-be-published"}, "sensitive_metadata"),
    ({"jwks": {"keys": [{"kty": "RSA", "d": "private-key"}]}}, "sensitive_metadata"),
])
async def test_verify_rejects_invalid_or_incompatible_metadata(db_app, monkeypatch, changes, code):
    remote(monkeypatch, document(**changes))
    result = await verify(db_app)
    assert result.status_code == 200, result.text
    assert result.json()["ok"] is False and result.json()["code"] == code
    assert "must-never-be-published" not in result.text and "private-key" not in result.text


@pytest.mark.parametrize("status,headers,raw,code", [
    (302, {"location": URL + "/new"}, b"", "redirect_not_allowed"),
    (200, {"content-type": "text/html"}, b"<html>login</html>", "not_json"),
    (200, {"content-type": "text/plain"}, b"{}", "invalid_content_type"),
    (200, {"content-type": "application/json"}, b"x" * 65537, "response_too_large"),
    (404, {}, b"", "http_error"),
], ids=["redirect", "html", "content-type", "oversize", "not-found"])
async def test_verify_http_failures_are_structured(db_app, monkeypatch, status, headers, raw, code):
    remote(monkeypatch, status=status, headers=headers, raw=raw)
    value = (await verify(db_app)).json()
    assert value["ok"] is False and value["code"] == code


@pytest.mark.parametrize("url,code", [
    ("http://example.com/c.json", "not_https"),
    ("https://example.com", "no_path"),
    ("https://user@example.com/c.json", "invalid_url"),
    ("https://example.com/c.json#fragment", "invalid_url"),
    ("https://127.0.0.1/c.json", "unsafe_address"),
    ("https://169.254.169.254/c.json", "unsafe_address"),
    ("https://example.com/a/../c.json", "invalid_url"),
])
async def test_verify_rejects_unsafe_urls_before_fetch(db_app, url, code):
    value = (await verify(db_app, url=url)).json()
    assert not value["ok"] and value["code"] == code


async def test_verification_is_server_owned_and_failure_replaces_success(db_app, monkeypatch):
    remote(monkeypatch)
    assert (await verify(db_app)).json()["ok"]
    status = await db_app.web.post("/api/v1/oauth/client-metadata/status",
        json={"source": "external", "url": URL})
    assert status.json()["status"] == "verified" and status.json()["verified_at"]
    remote(monkeypatch, document(client_id=URL + "/wrong"))
    assert not (await verify(db_app)).json()["ok"]
    assert (await db_app.web.post("/api/v1/oauth/client-metadata/status",
        json={"source": "external", "url": URL})).json()["status"] == "failed"


async def test_editor_draft_must_match_deployed_document(db_app, monkeypatch):
    remote(monkeypatch)
    value = (await verify(db_app, metadata=document(client_name="Edited locally"))).json()
    assert not value["ok"] and value["code"] == "document_mismatch"


async def test_external_failure_obeys_policy_without_falling_back_to_mmg(db_app, monkeypatch):
    calls = discovery(monkeypatch, cimd=True)
    remote(monkeypatch, status=404)
    db_app.app.state.config.public_url = "https://gateway.example.com"
    for mode in ["prefer_cimd_fallback_dcr", "cimd_only"]:
        row = await server(db_app, {"type": "oauth", "auth_mode": mode,
            "cimd_metadata_source": "external", "cimd_external_url": URL,
            "cimd_verified_at": 9999999999})
        result = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
        if mode == "cimd_only":
            assert result.status_code == 502 and "CIMD" in result.text
        else:
            assert result.status_code == 200
            assert parse_qs(urlsplit(result.json()["authorization_url"]).query)["client_id"] == ["registered-client"]
    assert len(calls) == 1


async def test_verifier_rate_limit_and_authentication(db_app, monkeypatch):
    remote(monkeypatch)
    for _ in range(5):
        assert (await verify(db_app)).status_code == 200
    assert (await verify(db_app)).status_code == 429
    async with httpx.AsyncClient(transport=httpx.ASGITransport(db_app.app), base_url="http://test") as anon:
        assert (await anon.post("/api/v1/oauth/client-metadata/verify",
            json={"source": "external", "url": URL})).status_code == 401


async def test_changed_callback_and_stale_verification_recheck(db_app, monkeypatch):
    from mcp_manager import oauth_metadata
    discovery(monkeypatch, cimd=True)
    fetched = remote(monkeypatch)
    row = await server(db_app, {"type": "oauth", "auth_mode": "cimd_only",
        "cimd_metadata_source": "external", "cimd_external_url": URL})
    result = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert result.status_code == 200
    monkeypatch.setattr(oauth_metadata, "VERIFICATION_TTL", 0)
    assert (await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")).status_code == 200
    assert len(fetched) == 2
    db_app.app.state.config.public_url = "http://localhost:8765"
    failed = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert failed.status_code == 502


def test_inactive_hosting_settings_do_not_change_registration_binding():
    from mcp_manager.oauth_config import comparable_settings
    base = {"type": "oauth", "auth_mode": "dcr_only", "client_name": "Codex"}
    extended = copy.deepcopy(base) | {"cimd_metadata_source": "external", "cimd_external_url": URL,
                                     "cimd_verified_at": 123}
    assert comparable_settings(base) == comparable_settings(extended)


async def test_personal_editor_can_restore_saved_external_config_without_tokens(db_app):
    row = await server(db_app, {"type": "oauth", "auth_mode": "cimd_only",
        "config_isolation": "user", "cimd_metadata_source": "external",
        "cimd_external_url": URL, "cimd_metadata": document()})
    response = await db_app.web.get(f"/api/v1/mcps/{row.id}/credentials/status")
    assert response.status_code == 200
    auth = response.json()["oauth_config"]
    assert auth["cimd_external_url"] == URL and auth["cimd_metadata"] == document()
    assert "access_token" not in auth and "refresh_token" not in auth


async def test_shared_oauth_settings_are_not_returned_as_personal_editor_config(db_app):
    row = await server(db_app)
    response = await db_app.web.get(f"/api/v1/mcps/{row.id}/credentials/status")
    assert response.status_code == 200
    assert "oauth_config" not in response.json()


@pytest.mark.parametrize("url", ["https://metadata.example.com/mmg.json#",
                                "https://metadata.example.com/a%2f..%2fmmg.json"])
async def test_url_syntax_is_checked_without_http_client_normalization(db_app, url):
    result = (await verify(db_app, url=url)).json()
    assert not result["ok"] and result["code"] == "invalid_url"


async def test_public_url_change_during_external_fetch_cannot_create_mismatched_flow(db_app, monkeypatch):
    import asyncio

    from mcp_manager import oauth_metadata
    discovery(monkeypatch, cimd=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def handle(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=document())

    monkeypatch.setattr(oauth_metadata, "http_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handle)))
    row = await server(db_app, {"type": "oauth", "auth_mode": "cimd_only",
        "cimd_metadata_source": "external", "cimd_external_url": URL})
    task = asyncio.create_task(db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    await asyncio.wait_for(entered.wait(), 5)
    db_app.app.state.config.public_url = "http://localhost:8765"
    release.set()
    response = await task
    assert response.status_code == 409
    assert not db_app.app.state.oauth.pending


@pytest.mark.parametrize("public_url", ["https://mmg.example.com/#", "https://mmg.example.com/?", "https://mmg.example.com/a/..",
                                       "https://mmg.example.com/a%2f.."])
async def test_template_rejects_callback_base_browser_would_reinterpret(db_app, public_url):
    db_app.app.state.config.public_url = public_url
    response = await db_app.web.get("/api/v1/oauth/client-metadata/template",
        params={"source": "external", "hosting_url": URL})
    assert response.status_code == 422


async def test_overflowing_json_number_is_structured_failure_and_clears_evidence(db_app, monkeypatch):
    import json

    remote(monkeypatch)
    assert (await verify(db_app)).json()["ok"]
    raw = json.dumps(document()).encode()[:-1] + b', "extension_number":1e999}'
    remote(monkeypatch, raw=raw)
    value = (await verify(db_app)).json()
    assert not value["ok"] and value["code"] == "invalid_metadata"
    status = await db_app.web.post("/api/v1/oauth/client-metadata/status",
        json={"source": "external", "url": URL})
    assert status.json()["status"] == "failed"


async def test_evidence_survives_verifier_recreation_and_is_scoped_by_actor(db_app, monkeypatch):
    from mcp_manager.oauth_metadata import CIMDVerifier

    remote(monkeypatch)
    assert (await verify(db_app)).json()["ok"]
    fresh = CIMDVerifier(db_app.app.state.catalog)
    assert (await fresh.status(db_app.actor["id"], URL, REDIRECT))["ok"]
    assert (await fresh.status("different-user", URL, REDIRECT))["status"] == "unverified"


async def test_auth_config_change_during_external_fetch_cannot_commit(db_app, monkeypatch):
    import asyncio

    from mcp_manager import oauth_metadata

    discovery(monkeypatch, cimd=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def handle(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=document())

    monkeypatch.setattr(oauth_metadata, "http_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handle)))
    row = await server(db_app, {"type": "oauth", "auth_mode": "cimd_only",
        "cimd_metadata_source": "external", "cimd_external_url": URL})
    task = asyncio.create_task(db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start"))
    await asyncio.wait_for(entered.wait(), 5)
    config = await db_app.app.state.credentials.materialize(row, db_app.actor["id"])
    config["auth"]["cimd_external_url"] = URL + "/changed"
    await db_app.app.state.catalog.update(row.id, {"config": config}, user_id=db_app.actor["id"])
    release.set()
    response = await task
    assert response.status_code == 409
    assert not db_app.app.state.oauth.pending


@pytest.mark.parametrize("mode", [[], {}])
def test_malformed_registration_mode_remains_a_validation_error(mode):
    from mcp_manager.transports import validate_config

    with pytest.raises(ValueError):
        validate_config("streamable-http", {"url": "https://mcp.test/mcp",
            "auth": {"type": "oauth", "auth_mode": mode}})


async def test_blocked_verification_attempt_is_audited_without_url(db_app, monkeypatch):
    events = []

    async def audit(action, actor, details):
        events.append((action, actor, details))

    monkeypatch.setattr(db_app.app.state.catalog.logs, "audit", audit)
    result = await verify(db_app, url="https://169.254.169.254/private.json")
    assert result.json()["code"] == "unsafe_address"
    assert events[0][0] == "oauth.cimd_verify" and events[0][1] == db_app.actor["id"]
    assert events[0][2]["code"] == "unsafe_address"
    assert "169.254.169.254" not in repr(events)


@pytest.mark.parametrize("name,want", [
    ("Codex", "Codex"), ("  MMG 中文 & agent  ", "MMG 中文 & agent"), ("", "Codex"), ("   ", "Codex"),
])
async def test_external_template_uses_current_form_client_name(db_app, name, want):
    response = await db_app.web.get("/api/v1/oauth/client-metadata/template", params={
        "source": "external", "hosting_url": URL, "callback_path": "/callback", "client_name": name})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "client_id": URL, "client_name": want,
        "redirect_uris": ["http://127.0.0.1:8765/callback"],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "application_type": "native",
    }


async def test_external_template_omitted_client_name_defaults_to_codex(db_app):
    response = await db_app.web.get("/api/v1/oauth/client-metadata/template",
        params={"source": "external", "hosting_url": URL})
    assert response.status_code == 200
    assert response.json()["client_name"] == "Codex"


async def test_edited_template_without_refresh_grant_is_preserved(db_app, monkeypatch):
    from mcp_manager.oauth_metadata import generate_client_metadata, validate_cimd_metadata

    edited = generate_client_metadata("http://127.0.0.1:8765", URL, "/api/v1/oauth/callback")
    edited["grant_types"] = ["authorization_code"]
    edited["application_type"] = "web"
    assert validate_cimd_metadata(edited, URL, REDIRECT) == edited
    remote(monkeypatch, edited)
    result = await verify(db_app, metadata=edited)
    assert result.status_code == 200 and result.json()["ok"], result.text
    assert edited["grant_types"] == ["authorization_code"]
    assert edited["application_type"] == "web"


@pytest.mark.parametrize("public_url,want", [
    ("http://127.0.0.1:8765", "native"),
    ("http://localhost:8765", "native"),
    ("http://[::1]:8765", "native"),
    ("https://gateway.example.com", "web"),
    ("https://192.168.1.2", "web"),
    ("https://localhost", "web"),
])
def test_generated_application_type_matches_callback(public_url, want):
    from mcp_manager.oauth_metadata import generate_client_metadata

    value = generate_client_metadata(public_url, URL, "/callback")
    assert value["application_type"] == want
