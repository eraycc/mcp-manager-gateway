"""Client registration discovery obeys issuer/resource boundaries and CIMD priority."""
import httpx
import pytest


def harness(monkeypatch, metadata=None, *, prm=None, registration_status=201):
    from mcp_manager import oauth_discovery as discovery

    calls = []
    resource = "https://mcp.test/mcp"
    issuer = "https://auth.test/tenant"
    metadata = metadata or {"issuer": issuer, "authorization_endpoint": issuer + "/authorize",
                            "token_endpoint": issuer + "/token", "registration_endpoint": issuer + "/register"}
    def handle(request):
        calls.append(request)
        assert "Authorization" not in request.headers
        url = str(request.url)
        if url == resource:
            return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer resource_metadata="https://mcp.test/meta", scope="read"'})
        if url == "https://mcp.test/meta":
            return httpx.Response(200, json=prm or {"resource": resource, "authorization_servers": [issuer]})
        if url == "https://auth.test/.well-known/oauth-authorization-server/tenant":
            return httpx.Response(200, json=metadata)
        if url.endswith("/register"):
            return httpx.Response(registration_status, json={"client_id": "registered", "token_endpoint_auth_method": "none"})
        return httpx.Response(404)
    monkeypatch.setattr(discovery, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return discovery, calls, resource


async def test_discovery_uses_prm_and_issuer_path_and_scope(monkeypatch):
    discovery, calls, resource = harness(monkeypatch)
    result = await discovery.discover(resource, {})
    assert result["issuer_url"] == "https://auth.test/tenant"
    assert result["resource"] == resource and result["scopes"] == ["read"]
    assert len(calls) == 3


@pytest.mark.parametrize("bad", [
    {"resource": "https://other.test/mcp", "authorization_servers": ["https://auth.test/tenant"]},
    {"resource": "https://mcp.test/mcp", "authorization_servers": ["http://auth.test/tenant"]},
])
async def test_unsafe_resource_metadata_cannot_fall_back_to_manual_endpoints(monkeypatch, bad):
    from mcp_manager.runtime import GatewayError
    discovery, _, resource = harness(monkeypatch, prm=bad)
    with pytest.raises(GatewayError):
        await discovery.discover(resource, {"authorization_url": "https://manual.test/a", "token_url": "https://manual.test/t"})


async def test_issuer_mismatch_is_rejected(monkeypatch):
    from mcp_manager.runtime import GatewayError
    discovery, _, resource = harness(monkeypatch, metadata={
        "issuer": "https://wrong.test", "authorization_endpoint": "https://wrong.test/a", "token_endpoint": "https://wrong.test/t"})
    with pytest.raises(GatewayError):
        await discovery.discover(resource, {})


async def test_cimd_metadata_is_public_information_and_requires_https_public_url():
    from mcp_manager.oauth_config import client_metadata
    from mcp_manager.runtime import GatewayError

    document = client_metadata("https://gateway.example.com")
    assert document["client_id"] == "https://gateway.example.com/oauth/client-metadata.json"
    assert document["redirect_uris"] == ["https://gateway.example.com/api/v1/oauth/callback"]
    assert not any("secret" in key or "token" in key and key != "token_endpoint_auth_method" for key in document)
    for url in ("http://127.0.0.1:8765", "https://localhost", "https://192.168.1.2"):
        with pytest.raises(GatewayError):
            client_metadata(url)
