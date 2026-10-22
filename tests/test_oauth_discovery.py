"""Client registration discovery obeys issuer/resource boundaries and CIMD priority."""
import httpx
import pytest


def harness(monkeypatch, metadata=None, *, prm=None, registration_status=201, resource="https://mcp.test/mcp"):
    from mcp_manager import oauth_discovery as discovery

    calls = []
    request_resource = str(httpx.URL(discovery.canonical_resource(resource)))
    issuer = "https://auth.test/tenant"
    metadata = metadata or {"issuer": issuer, "authorization_endpoint": issuer + "/authorize",
                            "token_endpoint": issuer + "/token", "registration_endpoint": issuer + "/register"}
    def handle(request):
        calls.append(request)
        assert "Authorization" not in request.headers
        url = str(request.url)
        if url == request_resource:
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
    assert document["redirect_uris"] == ["https://gateway.example.com/api/v1/oauth/callback",
                                         "https://gateway.example.com/callback"]
    assert not any("secret" in key or "token" in key and key != "token_endpoint_auth_method" for key in document)
    for url in ("http://127.0.0.1:8765", "https://localhost", "https://192.168.1.2"):
        with pytest.raises(GatewayError):
            client_metadata(url)


@pytest.mark.parametrize("resource,advertised", [
    ("https://mcp.vercel.com", "https://mcp.vercel.com/"),
    ("https://mcp.test/", "https://mcp.test"),
    ("HTTPS://MCP.TEST/Tools", "https://mcp.test/Tools/"),
    ("https://mcp.test/Tools/", "HTTPS://MCP.TEST/Tools"),
    ("https://mcp.test/Tools///", "https://mcp.test/Tools"),
    ("https://mcp.test/Tools/?version=One", "https://mcp.test/Tools?version=One"),
    ("https://mcp.test/Tools/?next=/", "https://mcp.test/Tools?next=/"),
])
async def test_prm_resource_normalization_accepts_only_requested_equivalences(
    monkeypatch, resource, advertised,
):
    discovery, _, _ = harness(monkeypatch, resource=resource, prm={
        "resource": advertised, "authorization_servers": ["https://auth.test/tenant"]})
    result = await discovery.discover(resource, {})
    assert result["resource"] == discovery.canonical_resource(resource)


@pytest.mark.parametrize("advertised", [
    "https://mcp.test/tools/",
    "https://mcp.test/",
    "https://mcp.test/Tools/child",
    "https://mcp.test/ToolsExtra",
    "https://other.test/Tools",
    "https://mcp.test:8443/Tools",
    "http://localhost/Tools",
    "https://mcp.test/Tools?version=One",
    "https://mcp.test/Tools?next=/",
    "https://mcp.test/%54ools",
    "https://mcp.test/Tools/../Tools",
    "https://mcp.test//Tools",
])
async def test_prm_resource_normalization_rejects_other_resources_and_logs_values(
    monkeypatch, caplog, advertised,
):
    from mcp_manager.runtime import GatewayError

    resource = "HTTPS://MCP.TEST/Tools/"
    discovery, calls, _ = harness(monkeypatch, resource=resource, prm={
        "resource": advertised, "authorization_servers": ["https://auth.test/tenant"]})
    with caplog.at_level("WARNING", logger="mcp_manager.oauth_discovery"), pytest.raises(GatewayError) as failure:
        await discovery.discover(resource, {
            "authorization_url": "https://manual.test/a", "token_url": "https://manual.test/t"})
    assert failure.value.code == "oauth_metadata_invalid"
    assert len(calls) == 2, "A mismatched PRM must stop before contacting its authorization server"
    record = next(record for record in caplog.records if "PRM resource mismatch" in record.message)
    assert record.args == (
        resource, advertised, "https://mcp.test/Tools",
        advertised.rstrip("/") if "?" not in advertised else advertised,
    )


@pytest.mark.parametrize("advertised", [
    "https://mcp.test/Other?api_key=private-key",
    "https://mcp.test/Other?access_token=private%2dtoken",
    "https://user:private-password@[bad]/Tools?api_key=private-key",
    "https://user:private-password@mcp.test/Tools",
    {"client_secret": "private-secret"},
])
async def test_prm_mismatch_log_redacts_credentials_without_changing_comparison(
    monkeypatch, caplog, advertised,
):
    from mcp_manager.runtime import GatewayError

    resource = "https://mcp.test/Tools/?access_token=private-token"
    discovery, _, _ = harness(monkeypatch, resource=resource, prm={
        "resource": advertised, "authorization_servers": ["https://auth.test/tenant"]})
    with caplog.at_level("WARNING", logger="mcp_manager.oauth_discovery"), pytest.raises(GatewayError) as failure:
        await discovery.discover(resource, {})
    assert failure.value.code == "oauth_metadata_invalid"
    assert "PRM resource mismatch" in caplog.text
    assert not any(secret in caplog.text for secret in (
        "private-token", "private%2dtoken", "private-key", "private-password", "private-secret"))
    assert "resource_raw=" in caplog.text and "resource_normalized=" in caplog.text
    assert "https://mcp.test/Tools/" in caplog.text


async def test_query_values_are_not_redacted_before_prm_comparison(monkeypatch, caplog):
    from mcp_manager.runtime import GatewayError

    resource = "https://mcp.test/Tools?access_token=first-secret"
    discovery, _, _ = harness(monkeypatch, resource=resource, prm={
        "resource": "https://mcp.test/Tools?access_token=other-secret",
        "authorization_servers": ["https://auth.test/tenant"]})
    with pytest.raises(GatewayError):
        await discovery.discover(resource, {})
    assert "first-secret" not in caplog.text and "other-secret" not in caplog.text
