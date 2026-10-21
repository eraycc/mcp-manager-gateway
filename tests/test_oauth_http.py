"""OAuth metadata DNS checks pin the actual connection and retain TLS identity."""
import asyncio
import socket

import httpx
import pytest

from mcp_manager import oauth_http
from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["192.168.0.5"], ["::1"], ["8.8.8.8", "10.0.0.1"]])
async def test_public_metadata_rejects_private_or_mixed_dns(monkeypatch, addresses):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    with pytest.raises(GatewayError) as error:
        await oauth_http.resolve_addresses("metadata.example.com", 443, 1)
    assert error.value.code == "oauth_metadata_invalid"


async def test_dns_is_pinned_without_losing_host_or_certificate_identity(monkeypatch):
    transport = oauth_http.OAuthHTTPTransport()
    calls = []

    async def resolve(*args):
        return ["8.8.8.8"]

    async def send(request):
        calls.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(oauth_http, "resolve_addresses", resolve)
    monkeypatch.setattr(transport.inner, "handle_async_request", send)
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://metadata.example.com/path", extensions={"oauth_public_only": True})
        assert calls[0].url.host == "8.8.8.8"
        assert calls[0].headers["host"] == "metadata.example.com"
        assert calls[0].extensions["sni_hostname"] == "metadata.example.com"
    finally:
        await transport.aclose()


async def test_multi_address_fallback_is_limited_to_connection_establishment(monkeypatch):
    transport = oauth_http.OAuthHTTPTransport()
    calls = []

    async def resolve(*args):
        return ["8.8.8.8", "1.1.1.1"]

    async def send(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, json={})

    monkeypatch.setattr(oauth_http, "resolve_addresses", resolve)
    monkeypatch.setattr(transport.inner, "handle_async_request", send)
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("https://auth.example.com/register", json={}, extensions={"oauth_public_only": True})
    assert calls == ["8.8.8.8", "1.1.1.1"]


@pytest.mark.parametrize("register", [False, True])
async def test_metadata_total_deadline_preserves_registration_unknown_outcome(monkeypatch, register):
    from mcp_manager import oauth_discovery

    async def handle(request):
        await asyncio.sleep(.1)
        return httpx.Response(200, json={})

    monkeypatch.setattr(oauth_discovery, "REQUEST_TIMEOUT", .01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        expected = GatewayError if register else oauth_discovery.Unavailable
        with pytest.raises(expected) as error:
            await oauth_discovery.json_request(client, "https://auth.test/meta", "https://mcp.test/mcp",
                                               data={} if register else None)
    if register:
        assert error.value.code == "oauth_registration_unknown"
