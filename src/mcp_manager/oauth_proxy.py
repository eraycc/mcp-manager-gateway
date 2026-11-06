"""Retain the TLS name while HTTP CONNECT uses an already validated OAuth IP."""
import httpx


class _TLSStream:
    def __init__(self, stream, hostname, proxy_tls):
        self.stream, self.hostname, self.proxy_tls = stream, hostname, proxy_tls

    def __getattr__(self, name):
        return getattr(self.stream, name)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # HTTPS proxies negotiate their own certificate before the target tunnel.
        hostname = server_hostname if self.proxy_tls else self.hostname
        stream = await self.stream.start_tls(ssl_context, server_hostname=hostname, timeout=timeout)
        return _TLSStream(stream, self.hostname, False)


class _TLSBackend:
    def __init__(self, backend, hostname, proxy_tls):
        self.backend, self.hostname, self.proxy_tls = backend, hostname, proxy_tls

    def __getattr__(self, name):
        return getattr(self.backend, name)

    async def connect_tcp(self, *args, **kwargs):
        stream = await self.backend.connect_tcp(*args, **kwargs)
        return _TLSStream(stream, self.hostname, self.proxy_tls)


class PinnedProxyTransport(httpx.AsyncBaseTransport):
    def __init__(self, module, hostname, *, proxy_tls, **kwargs):
        self.inner = module.AsyncHTTPTransport(**kwargs)
        # HTTPcore CONNECT currently ignores sni_hostname on the target hop.
        # HTTPX exposes no network_backend argument. Keep this private adapter
        # confined here and cover both TLS hops with real CONNECT/TLS tests.
        pool = self.inner._pool
        pool._network_backend = _TLSBackend(pool._network_backend, hostname, proxy_tls)

    async def handle_async_request(self, request):
        # Otherwise HTTPS proxy TLS would incorrectly use the target hostname.
        extensions = {key: value for key, value in request.extensions.items() if key != "sni_hostname"}
        forwarded = httpx.Request(request.method, request.url, headers=request.headers,
                                  stream=request.stream, extensions=extensions)
        return await self.inner.handle_async_request(forwarded)

    async def aclose(self):
        await self.inner.aclose()
