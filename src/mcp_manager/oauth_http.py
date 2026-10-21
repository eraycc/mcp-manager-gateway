"""Pin discovered public OAuth requests to validated DNS addresses."""
import asyncio
import ipaddress
import socket

import httpx

from .runtime import GatewayError


async def resolve_addresses(host, port, timeout):
    try:
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=timeout,
        )
    except (OSError, TimeoutError):
        raise httpx.ConnectError("OAuth endpoint DNS resolution failed") from None
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise GatewayError("oauth_metadata_invalid", "Public OAuth metadata cannot access private network addresses")
    return addresses


class OAuthHTTPTransport(httpx.AsyncBaseTransport):
    """Keep Host/TLS identity while preventing DNS rebinding into private networks."""

    def __init__(self):
        # Pinned IPs can belong to multiple TLS hosts: never reuse across identities.
        self.inner = httpx.AsyncHTTPTransport(
            trust_env=False, limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def handle_async_request(self, request):
        if request.extensions.get("oauth_public_only"):
            host = request.url.host
            timeout = request.extensions.get("timeout", {}).get("connect")
            addresses = await resolve_addresses(host, request.url.port or 443, timeout or 8)
            for index, address in enumerate(addresses):
                pinned = httpx.Request(
                    request.method, request.url.copy_with(host=address),
                    headers=request.headers, stream=request.stream,
                    extensions=request.extensions | {"sni_hostname": host},
                )
                try:
                    return await self.inner.handle_async_request(pinned)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    # Only connection establishment failures precede sending any POST bytes.
                    if index == len(addresses) - 1:
                        raise
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()
