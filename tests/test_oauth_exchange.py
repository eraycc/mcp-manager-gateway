"""Token exchange validates public/confidential clients without exposing response secrets."""
import base64
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from mcp_manager.oauth import OAuth
from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize(("method", "secret"), [("none", ""), ("client_secret_basic", "secret:with+symbols"), ("client_secret_post", "secret")])
async def test_exchange_uses_one_configured_client_auth_method(monkeypatch, method, secret):
    from mcp_manager import oauth
    calls = []
    def handle(request):
        calls.append(request)
        data = parse_qs(request.content.decode())
        if method == "client_secret_basic":
            assert "client_secret" not in data and "client_id" not in data
            decoded = base64.b64decode(request.headers["authorization"].split()[1]).decode()
            identity, password = decoded.split(":", 1)
            assert unquote(identity) == "client:id" and unquote(password) == secret
        else:
            assert data["client_id"] == ["client:id"]
            assert ("client_secret" in data) == bool(secret)
        return httpx.Response(200, json={"access_token": "access", "expires_in": 3600, "token_type": "Bearer"})
    monkeypatch.setattr(oauth, "token_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    token = await OAuth(None).exchange({"client_id": "client:id", "client_secret": secret,
        "token_url": "https://auth.test/token", "token_endpoint_auth_method": method}, {"grant_type": "authorization_code", "code": "code"})
    assert token["access_token"] == "access" and token["expires_at"] > 0 and len(calls) == 1


@pytest.mark.parametrize("payload", [[], {"access_token": 12}, {"access_token": ""},
    {"access_token": "x", "expires_in": True}, {"access_token": "x", "expires_in": "NaN"},
    {"access_token": "x", "expires_in": 10**309}, {"access_token": "x", "refresh_token": []}, {"access_token": "x", "token_type": "mac"}])
async def test_bad_token_response_is_a_safe_oauth_error(monkeypatch, payload):
    from mcp_manager import oauth
    monkeypatch.setattr(oauth, "token_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=payload))))
    with pytest.raises(GatewayError) as error:
        await OAuth(None).exchange({"token_url": "https://auth.test/token", "client_id": "id"}, {})
    assert error.value.code == "oauth_failed"


@pytest.mark.parametrize("error_value", [[], {}, "private-provider-error"])
async def test_non_string_or_private_error_never_escapes_token_validation(monkeypatch, error_value):
    from mcp_manager import oauth

    monkeypatch.setattr(oauth, "token_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": error_value, "error_description": "provider-secret"}))))
    with pytest.raises(GatewayError) as error:
        await OAuth(None).exchange({"token_url": "https://auth.test/token"}, {})
    assert error.value.code == "oauth_failed"
    assert "provider-secret" not in str(error.value) and "private-provider-error" not in str(error.value)


async def test_token_request_has_a_total_deadline(monkeypatch):
    import asyncio

    from mcp_manager import oauth

    async def handle(request):
        await asyncio.sleep(.1)
        return httpx.Response(200, json={"access_token": "late"})

    monkeypatch.setattr(oauth, "TOKEN_REQUEST_TIMEOUT", .01)
    monkeypatch.setattr(oauth, "token_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(GatewayError) as error:
        await OAuth(None).exchange({"token_url": "https://auth.test/token"}, {})
    assert error.value.code == "oauth_failed"
