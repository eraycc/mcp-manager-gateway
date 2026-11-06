"""Platform-independent outbound policy and per-request client leases."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import ssl
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import getproxies_environment

import anyio
import httpx

from .database import get_setting
from .outbound_rules import matches
from .outbound_rules import parse_rule as _entry

DEFAULTS = {
    "enabled": False, "mode": "disabled", "scheme": "http", "host": "", "port": 8080,
    "auth_type": "none", "username": None, "password": None,
    "list_mode": "blacklist", "list_entries": [], "ssl_verify": "default",
    "custom_ca_path": None, "validation_url": "https://example.com/", "version": 0,
}
REDACTED = "[REDACTED]"
_current_loader = ContextVar("outbound_config_loader", default=None)


def _endpoint(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise ValueError("validation_url must be an HTTP(S) URL without credentials")
    _ = parsed.port
    return parsed


def normalize_config(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("Unknown outbound proxy setting")
    result = deepcopy(DEFAULTS) | value
    if type(result["enabled"]) is not bool:
        raise ValueError("enabled must be a boolean")
    enums = {"mode": {"disabled", "manual", "env"}, "scheme": {"http", "https", "socks5", "socks5h"},
             "auth_type": {"none", "basic"}, "list_mode": {"blacklist", "whitelist"},
             "ssl_verify": {"default", "custom_ca", "insecure"}}
    for key, allowed in enums.items():
        if not isinstance(result[key], str) or result[key] not in allowed:
            raise ValueError(key + " is invalid")
    if not result["enabled"]:
        result["mode"] = "disabled"
    elif result["mode"] == "disabled":
        raise ValueError("Choose manual or env mode when enabling the proxy")
    for key in ("host", "username", "password", "custom_ca_path", "validation_url"):
        item = result[key]
        if item is not None and (not isinstance(item, str) or len(item) > 4096 or re.search(r"[\x00-\x1f\x7f]", item)):
            raise ValueError(key + " must be a string without control characters")
    result["host"] = (result["host"] or "").strip()
    if type(result["port"]) is not int or not 1 <= result["port"] <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    if result["mode"] == "manual":
        host = result["host"]
        if not host or any(char in host for char in "/@?# \\"):
            raise ValueError("host must be a hostname or IP without a scheme or path")
        try:
            if ":" in host:
                ipaddress.IPv6Address(host.strip("[]"))
            else:
                if not re.fullmatch(r"[A-Za-z0-9_\-.]+", host.encode("idna").decode()):
                    raise ValueError
        except (ValueError, UnicodeError):
            raise ValueError("host is invalid") from None
        if result["auth_type"] == "basic" and not (result["username"] or "").strip():
            raise ValueError("basic authentication requires username")
        parsed = urlsplit(proxy_url(result))
        _ = parsed.port
    entries = result["list_entries"]
    if not isinstance(entries, list) or len(entries) > 500:
        raise ValueError("list_entries must be an array with at most 500 entries")
    result["list_entries"] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip() or len(entry) > 2048 or re.search(r"\s", entry.strip()):
            raise ValueError("list_entries cannot contain empty or invalid entries")
        entry = entry.strip()
        _entry(entry)
        if entry not in result["list_entries"]:
            result["list_entries"].append(entry)
    _endpoint(result["validation_url"])
    if result["ssl_verify"] == "custom_ca" and not result["custom_ca_path"]:
        raise ValueError("custom_ca_path is required")
    if type(result["version"]) is not int or result["version"] < 0:
        raise ValueError("version must be a nonnegative integer")
    return result


def proxy_url(config):
    host = config["host"]
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    auth = ""
    if config["auth_type"] == "basic" and config.get("username"):
        auth = quote(config["username"], safe="")
        if config.get("password"):
            auth += ":" + quote(config["password"], safe="")
        auth += "@"
    return f'{config["scheme"]}://{auth}{host}:{config["port"]}'


def client_options(config, url, *, include_tls=True):
    options = {"trust_env": False}
    mode = config["mode"] if config["enabled"] else "disabled"
    if mode == "disabled":
        return options
    if mode == "env":
        # Read only process variables. urllib.getproxies() can fall back to OS settings.
        env = getproxies_environment()
        bypass = matches(url, [x.strip() for x in env.get("no", "").split(",") if x.strip()], no_proxy=True)
        proxy = None if bypass else env.get(urlsplit(str(url)).scheme, env.get("all"))
        if proxy:
            options.update(proxy=proxy, trust_env=True)
    elif mode == "manual":
        hit = matches(url, config["list_entries"])
        if (config["list_mode"] == "whitelist") == hit:
            options["proxy"] = proxy_url(config)
    if not include_tls:
        return options
    if config["ssl_verify"] == "insecure":
        options["verify"] = False
    elif config["ssl_verify"] == "custom_ca":
        options["verify"] = ssl.create_default_context(cafile=str(Path(config["custom_ca_path"])))
    return options


def build_client(config, url, module=httpx, *, sni_hostname=None, **kwargs):
    options = client_options(config, url)
    options = kwargs | options
    # HTTPS proxies have a TLS connection of their own in addition to the target.
    proxy = options.get("proxy")
    verify = options.get("verify", True)
    if proxy and urlsplit(proxy).scheme == "https" and verify is not True:
        if verify is False:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = verify
        options["proxy"] = module.Proxy(proxy, ssl_context=context)
    if sni_hostname and proxy and urlsplit(proxy).scheme in {"http", "https"}:
        from .oauth_proxy import PinnedProxyTransport

        transport_options = {key: options.pop(key) for key in
                             ("proxy", "verify", "cert", "trust_env", "http1", "http2", "limits") if key in options}
        options["transport"] = PinnedProxyTransport(
            module, sni_hostname, proxy_tls=urlsplit(proxy).scheme == "https", **transport_options)
        # The explicit transport owns proxy selection, including env-mode proxies.
        options["trust_env"] = False
    return module.AsyncClient(**options)


async def load_config(state):
    stored = await get_setting(state.db, "outbound_proxy")
    if stored is None:
        return deepcopy(DEFAULTS)
    # Decryption failures must fail closed, never quietly fall back to direct.
    return normalize_config(state.catalog.unseal(stored))


class _ClientPool:
    def __init__(self, client):
        self.client, self.users = client, 0
        self.retired = self.closed = False

    async def close_idle(self):
        if self.retired and not self.users and not self.closed:
            self.closed = True
            await self.client.aclose()

    async def release(self):
        self.users -= 1
        await self.close_idle()


class OutboundTransport(httpx.AsyncBaseTransport):
    """Reuse pools per policy; retire old pools only after their streams finish."""

    def __init__(self, loader=None, *, module=httpx, builder=None, **kwargs):
        self.loader = loader if loader is not None else _current_loader.get()
        self.module, self.builder, self.kwargs = module, builder, kwargs
        self.config, self.pools = None, {}

    async def handle_async_request(self, request):
        config = await self.loader() if self.loader else deepcopy(DEFAULTS)
        route_url = request.extensions.get("outbound_original_url", str(request.url))
        options = client_options(config, route_url, include_tls=False)
        retiring = []
        if config != self.config:
            retiring = list(self.pools.values())
            for pool in retiring:
                pool.retired = True
            self.pools = {}
            self.config = deepcopy(config)
        hostname = request.extensions.get("sni_hostname") if "outbound_original_url" in request.extensions else None
        key = (options.get("proxy"), options["trust_env"], hostname)
        if key not in self.pools:
            try:
                kwargs = self.kwargs | ({"sni_hostname": hostname} if hostname else {})
                client = (self.builder or build_client)(config, route_url, self.module, **kwargs)
            except BaseException:
                with anyio.CancelScope(shield=True):
                    for old in retiring:
                        await old.close_idle()
                raise
            self.pools[key] = _ClientPool(client)
        pool = self.pools[key]
        pool.users += 1
        try:
            for old in retiring:
                with anyio.CancelScope(shield=True):
                    await old.close_idle()
            response = await pool.client.send(request, stream=True, follow_redirects=False)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await pool.release()
            raise
        original = response.stream
        module = self.module
        released = False

        class LeasedStream(module.AsyncByteStream):
            async def __aiter__(self):
                async for chunk in original:
                    yield chunk

            async def aclose(self):
                nonlocal released
                if released:
                    return
                released = True
                with anyio.CancelScope(shield=True):
                    try:
                        await original.aclose()
                    finally:
                        await pool.release()

        response.stream = LeasedStream()
        return response

    async def aclose(self):
        pools, self.pools = list(self.pools.values()), {}
        for pool in pools:
            pool.retired = True
        with anyio.CancelScope(shield=True):
            for pool in pools:
                await pool.close_idle()


def get_outbound_client(*, module=httpx, loader=None, **kwargs):
    # Explicit test/in-process transports stay local; production uses the routing factory.
    transport = kwargs.pop("transport", None)
    kwargs.pop("trust_env", None)
    verify = kwargs.pop("verify", True)
    kwargs.pop("proxy", None)
    if transport is None:
        transport = OutboundTransport(loader, module=module, verify=verify)
    return module.AsyncClient(transport=transport, trust_env=False, **kwargs)


class OutboundContextMiddleware:
    def __init__(self, app, root_app):
        self.app, self.root_app = app, root_app

    async def __call__(self, scope, receive, send):
        token = _current_loader.set(lambda: load_config(self.root_app.state))
        try:
            await self.app(scope, receive, send)
        finally:
            _current_loader.reset(token)


class ProxyDiagnosticError(ValueError):
    """Safe diagnostic message with no raw network exception details."""


async def validate_connection(config):
    # Always probe the proxy itself, even when the target is absent from a whitelist.
    probe = dict(config, list_mode="blacklist", list_entries=[])
    try:
        async with asyncio.timeout(5):
            async with build_client(probe, config["validation_url"], timeout=5, follow_redirects=False) as client:
                async with client.stream("GET", config["validation_url"]) as response:
                    status = response.status_code
        if status == 407:
            raise ProxyDiagnosticError("代理认证失败，请检查用户名/密码")
        if not 200 <= status < 400:
            raise ProxyDiagnosticError("代理连接成功但无法访问校验目标，请检查代理出站策略或校验 URL")
    except httpx.ProxyError:
        raise ProxyDiagnosticError("代理认证或隧道建立失败，请检查用户名/密码及代理出站策略") from None
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        current = exc
        while current is not None:
            if isinstance(current, ssl.SSLError):
                raise ProxyDiagnosticError("SSL 证书验证失败，可配置自定义 CA 或临时关闭验证") from None
            current = current.__cause__
        raise ProxyDiagnosticError("无法连接到代理服务器，请检查 host/port 及代理服务是否运行") from None
    except (TimeoutError, httpx.TimeoutException):
        raise ProxyDiagnosticError("代理校验超时，请检查代理服务及出站策略") from None
    except (ssl.SSLError, OSError):
        raise ProxyDiagnosticError("SSL 证书或自定义 CA 无效，请检查证书配置") from None
    except httpx.HTTPError:
        raise ProxyDiagnosticError("代理连接成功但无法访问校验目标，请检查代理出站策略") from None
    except ImportError:
        raise ProxyDiagnosticError("SOCKS 代理需要安装 httpx[socks] 依赖") from None
