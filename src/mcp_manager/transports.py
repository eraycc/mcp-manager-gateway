"""Transport plugins normalize wire data without flattening MCP content."""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import entry_points
from urllib.parse import quote, urlsplit

import anyio
import httpx
import httpx2
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from .environment import connection_config, environment_value, stdio_environment
from .runtime import GatewayError, ServerSpec

BUILTIN_TRANSPORTS = {"stdio", "streamable-http", "sse", "rest"}
PLUGINS = {}


MAX_TOOL_RESPONSE_BYTES = 8 * 1024 * 1024


async def read_response_with_budget(response, max_bytes=MAX_TOOL_RESPONSE_BYTES) -> bytes:
    """Read an HTTP response incrementally and stop before unbounded buffering."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("Response byte budget must be a positive integer")
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise GatewayError(
                    "outcome_unknown",
                    "Downstream response exceeded the byte budget after dispatch",
                )
        except ValueError:
            raise GatewayError(
                "outcome_unknown",
                "Downstream returned an invalid Content-Length after dispatch",
            ) from None
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise GatewayError(
                "outcome_unknown",
                "Downstream response exceeded the byte budget after dispatch",
            )
    return bytes(body)


def register_transport(name, plugin):
    """Plugin exposes validate(config) and async-context-manager connect(spec)."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", name) or name in BUILTIN_TRANSPORTS or name in PLUGINS:
        raise ValueError("Transport plugin name is invalid or already registered")
    if not callable(getattr(plugin, "validate", None)) or not callable(getattr(plugin, "connect", None)):
        raise TypeError("Plugin requires validate and connect")
    PLUGINS[name] = plugin


def load_transport_plugins():
    for entry in entry_points(group="mcp_manager.transports"):
        if entry.name not in PLUGINS:
            register_transport(entry.name, entry.load())


def wire(value):
    return value.model_dump(by_alias=True, exclude_none=True) if hasattr(value, "model_dump") else value


def pointer(value, path):
    if path in ("", None):
        return value
    if not path.startswith("/"):
        raise ValueError("Response pointer must be an RFC 6901 JSON pointer")
    for piece in path[1:].split("/"):
        key = piece.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def substitute(value, args, *, url=False):
    if isinstance(value, dict):
        return {k: substitute(v, args, url=url) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, args, url=url) for v in value]
    if not isinstance(value, str):
        return value
    exact = re.fullmatch(r"\{([A-Za-z0-9_.-]+)\}", value)
    def lookup(key):
        current = args
        try:
            for piece in key.split("."):
                current = current[int(piece)] if isinstance(current, list) else current[piece]
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise ValueError("Missing or invalid REST argument path: " + key) from exc
        return current
    if exact and not url:
        return copy.deepcopy(lookup(exact[1]))
    def replace(match):
        result = str(lookup(match[1]))
        return quote(result, safe="") if url else result
    return re.sub(r"\{([A-Za-z0-9_.-]+)\}", replace, value)


def auth_headers(config):
    config = connection_config(config)
    headers = dict(config.get("headers", {}))
    headers.update({key: environment_value(name) for key, name in config.get("env_headers", {}).items()})
    auth = config.get("auth", {})
    if auth.get("type") == "bearer":
        headers["Authorization"] = "Bearer " + (environment_value(auth["token_env"])
                                                 if auth.get("token_env") else auth.get("token", ""))
    elif auth.get("type") == "basic":
        raw = (auth.get("username", "") + ":" + auth.get("password", "")).encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    elif auth.get("type") == "api_key":
        headers[auth.get("header", "X-API-Key")] = auth.get("value", "")
    elif auth.get("type") == "oauth":
        token = auth.get("access_token")
        if not token:
            raise GatewayError("auth_required", "Complete OAuth authorization in the Web console")
        headers["Authorization"] = "Bearer " + token
    return headers


def validate_config(transport, config):
    if not isinstance(config, dict):
        raise TypeError("MCP config must be an object")
    if not isinstance(config.get("auth", {}), dict):
        raise TypeError("auth must be an object")
    for key in ("headers", "env", "env_headers"):
        value = config.get(key, {})
        if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                              for k, v in value.items()):
            raise ValueError(key + " must be a string-to-string object")
    if config.get("auth", {}).get("type", "none") not in {"none", "bearer", "basic", "api_key", "oauth"}:
        raise ValueError("Unsupported authentication type")
    auth = config.get("auth", {})
    if auth.get("type") == "oauth":
        from .oauth_config import validate_oauth

        validate_oauth(auth, transport)
    auth_header = (auth.get("header", "X-API-Key") if auth.get("type") == "api_key" else
                   "Authorization" if auth.get("type") in {"oauth", "basic", "bearer"} else None)
    if auth_header:
        if not isinstance(auth_header, str) or not re.fullmatch(r"[!#$%&'*+.^_\x60|~0-9A-Za-z-]+", auth_header):
            raise ValueError("Authentication header name is invalid")
        if any(key.lower() == auth_header.lower() for key in
               list(config.get("headers", {})) + list(config.get("env_headers", {}))):
            raise ValueError("Authentication header conflicts with custom headers; configure it in one place")
    if transport in PLUGINS:
        PLUGINS[transport].validate(config)
        return
    if transport == "stdio":
        if not isinstance(config.get("command"), str) or not config["command"].strip():
            raise ValueError("stdio requires command")
        if not isinstance(config.get("args", []), list) or any(not isinstance(x, str) for x in config.get("args", [])):
            raise ValueError("args must be an array")
    elif transport in ("streamable-http", "sse"):
        url = config.get("url", "")
        # Validate reference syntax here; the resolved URL is checked again at connect.
        if isinstance(url, str) and config.get("environment_expansion") == "claude":
            url = re.sub(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}", "https://environment.invalid", url)
        elif isinstance(url, str) and config.get("environment_expansion") == "dsh" and re.fullmatch(
                r"\$[A-Za-z_][A-Za-z0-9_]*", url):
            url = "https://environment.invalid"
        check_url(url)
    elif transport == "rest":
        if config.get("healthcheck_url"):
            check_url(config["healthcheck_url"])
        tools = config.get("tools", [])
        if not isinstance(tools, list) or not tools:
            raise ValueError("REST requires at least one tool")
        names = set()
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise TypeError("Every REST tool requires a string name")
            if not tool.get("name") or tool["name"] in names:
                raise ValueError("REST tool names must be nonempty and unique")
            names.add(tool["name"])
            Draft202012Validator.check_schema(tool.get("inputSchema", {"type": "object"}))
            request = tool.get("request", {})
            if not isinstance(request, dict) or not isinstance(tool.get("response", {}), dict):
                raise TypeError("REST request and response must be objects")
            check_url(request.get("url", ""), template=True)
            if request.get("method", "GET").upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                raise ValueError("Unsupported REST HTTP method")
            if "json" in request and "body" in request:
                raise ValueError("Use json or body, not both")
    else:
        raise ValueError("Unsupported transport plugin")
    for key in ("enabled_tools", "disabled_tools", "env_vars"):
        if key in config and (not isinstance(config[key], list) or any(
                not isinstance(x, str) and not (key == "env_vars" and isinstance(x, dict))
                for x in config[key])):
            raise ValueError(key + " must be an array")
    for key in ("startup_timeout", "call_timeout", "stop_timeout", "queue_timeout"):
        if key in config and (not isinstance(config[key], (int, float)) or config[key] <= 0):
            raise ValueError(key + " must be positive")
    for key, value in config.get("headers", {}).items():
        if "\r" in str(key) + str(value) or "\n" in str(key) + str(value):
            raise ValueError("Headers cannot contain line breaks")


def validate_service_config(transport, isolation, config):
    """Validate transport configuration together with its instance isolation."""
    validate_config(transport, config)
    if isolation not in {"service", "user", "session"}:
        raise ValueError("Invalid isolation")
    if isolation == "session" and transport != "stdio":
        raise ValueError("session isolation is only valid for stdio")
    auth = config.get("auth", {})
    if auth.get("type") == "oauth":
        value = auth.get("config_isolation", "shared")
        if value not in {"shared", "user"}:
            raise ValueError("OAuth config_isolation must be shared or user")
        if isolation != "user" and value != "shared":
            raise ValueError("OAuth config_isolation must be shared outside user isolation")


def check_url(url, *, template=False):
    if not isinstance(url, str):
        raise TypeError("URL must be a string")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL must use http or https")
    if parsed.username or parsed.password:
        raise ValueError("Put credentials in authentication settings, not in the URL")
    if template and ("{" in parsed.netloc or "}" in parsed.netloc):
        raise ValueError("REST URL host cannot be supplied by tool arguments")
    return parsed


def tool_allowed(config, name):
    allowed = config.get("enabled_tools")
    denied = config.get("disabled_tools", [])
    overrides = config.get("tools", {})
    override = overrides.get(name, {}) if isinstance(overrides, dict) else {}
    enabled = not isinstance(override, dict) or override.get("enabled", True)
    return enabled and name not in denied and (allowed is None or name in allowed)


@dataclass
class DiscoveryBudget:
    max_entries: int = 50000
    max_pages: int = 2000
    max_decoded_bytes: int = 128 * 1024 * 1024
    max_message_bytes: int = 8 * 1024 * 1024
    batch_size: int = 250
    entries: int = 0
    pages: int = 0
    decoded_bytes: int = 0

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in (
            self.max_entries, self.max_pages, self.max_decoded_bytes, self.max_message_bytes, self.batch_size
        )) or self.batch_size > 250:
            raise ValueError("Discovery budgets must be positive integers; batch_size must not exceed 250")

    @classmethod
    def from_config(cls, config):
        fields = ("max_entries", "max_pages", "max_decoded_bytes", "max_message_bytes")
        return cls(**{name: config["discovery_" + name] for name in fields if "discovery_" + name in config})

    def before_page(self):
        if self.pages >= self.max_pages:
            raise GatewayError("discovery_budget_exceeded", "Capability discovery exceeded its page budget")

    async def consume(self, result, entries):
        self.before_page()
        self.pages += 1
        self.entries += entries
        if self.entries > self.max_entries:
            raise GatewayError("discovery_budget_exceeded", "Capability discovery exceeded its entry budget")
        await self.consume_bytes(result)

    async def consume_bytes(self, result):
        """Count message and total bytes without charging a capability-list page."""
        def measure():
            size = 0
            for piece in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(result):
                size += len(piece.encode("utf-8"))
                if size > self.max_message_bytes:
                    raise GatewayError("discovery_budget_exceeded", "Capability page exceeded its message byte budget")
            return size
        self.decoded_bytes += await asyncio.to_thread(measure)
        if self.decoded_bytes > self.max_decoded_bytes:
            raise GatewayError("discovery_budget_exceeded", "Capability discovery exceeded its decoded byte budget")


async def iter_capabilities(connection, *, budget):
    iterator = getattr(connection, "iter_capabilities", None)
    if iterator:
        async for kind, entries in iterator(budget=budget):
            if kind == "_metadata":
                await budget.consume_bytes(entries[0] if entries else {})
            yield kind, entries
        return
    # Legacy transport plugins remain compatible; streaming plugins should expose iter_capabilities.
    result = await connection.discover()
    await budget.consume(result, sum(len(result.get(kind, [])) for kind in ("tools", "resources", "prompts", "templates")))
    for kind in ("tools", "resources", "prompts", "templates"):
        entries = result.get(kind, [])
        for offset in range(0, len(entries), budget.batch_size):
            yield kind, entries[offset:offset + budget.batch_size]
    yield "_metadata", [{key: result.get(key) for key in ("serverInfo", "protocolVersion", "capability_errors")}]


async def ingest_capabilities(connection, store, scope, *, budget, authorize=None):
    generation = await store.begin_refresh(scope)
    metadata = {}
    try:
        async for kind, entries in iter_capabilities(connection, budget=budget):
            if authorize:
                await authorize()
            if kind == "_error":
                for error in entries:
                    await store.discard_kind(generation.id, error["capability"])
            elif kind == "_metadata":
                metadata = entries[0] if entries else {}
            else:
                await store.stage_page(generation.id, kind, entries)
        if authorize:
            await authorize()
        await store.set_metadata(generation.id, metadata)
        await store.publish(generation.id, expected_scope=scope)
        return await store.get_summary(scope)
    except BaseException:
        # Cancellation/failure must not leave a partially ingested generation ready.
        await asyncio.shield(store.fail_refresh(generation.id))
        raise


class CapabilityConnection:
    async def ingest_catalog(self, store, scope, authorize=None):
        return await ingest_capabilities(
            self, store, scope, budget=DiscoveryBudget.from_config(self.config), authorize=authorize
        )


class PluginConnection(CapabilityConnection):
    def __init__(self, connection, config):
        self.connection, self.config = connection, config

    def __getattr__(self, name):
        return getattr(self.connection, name)


class RestConnection(CapabilityConnection):
    def __init__(self, config, client):
        self.config = config
        self.client = client

    async def _probe(self):
        headers = auth_headers(self.config)
        health = self.config.get("healthcheck_url")
        targets = [(health, headers)] if health else [
            (tool["request"]["url"], headers | {
                key: str(value) for key, value in tool["request"].get("headers", {}).items()
                if "{" not in str(value)})
            for tool in self.config.get("tools", []) if tool_allowed(self.config, tool["name"])]
        seen, unique = set(), []
        for url, request_headers in targets:
            if "{" in url:
                raise GatewayError("healthcheck_required", "REST routes with URL parameters require a fixed healthcheck_url")
            key = (url, tuple(sorted(request_headers.items())))
            if key not in seen:
                seen.add(key)
                unique.append((url, request_headers))
        if not health and len(unique) > 64:
            raise GatewayError("healthcheck_required", "More than 64 REST routes require a fixed healthcheck_url")
        method = "GET" if health else "HEAD"
        async def probe(target):
            url, request_headers = target
            try:
                async with self.client.stream(method, url, headers=request_headers) as response:
                    status = response.status_code
            except httpx.RequestError as exc:
                raise GatewayError("connection_error",
                    f"REST {method} connection failed ({type(exc).__name__}): {exc}") from exc
            if not (200 <= status < 300 or (not health and status == 405)):
                raise GatewayError("healthcheck_failed", f"REST {method} probe returned HTTP {status}")
        for offset in range(0, len(unique), 4):
            tasks = [asyncio.create_task(probe(target)) for target in unique[offset:offset + 4]]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

    async def iter_capabilities(self, *, budget):
        await self._probe()
        page = []
        for tool in self.config.get("tools", []):
            if not tool_allowed(self.config, tool["name"]):
                continue
            page.append({key: value for key, value in tool.items() if key in (
                "name", "description", "inputSchema", "outputSchema", "annotations", "title")}
                | {"inputSchema": tool.get("inputSchema", {"type": "object"})})
            if len(page) == budget.batch_size:
                await budget.consume({"tools": page}, len(page))
                yield "tools", page
                page = []
        if page:
            await budget.consume({"tools": page}, len(page))
            yield "tools", page
        yield "_metadata", [{"capability_errors": []}]

    async def discover(self):
        result = {"tools": [], "resources": [], "prompts": [], "templates": []}
        async for kind, entries in iter_capabilities(self, budget=DiscoveryBudget.from_config(self.config)):
            if kind == "_metadata":
                continue
            result[kind].extend(entries)
        return result

    async def call(self, name, arguments):
        if not tool_allowed(self.config, name):
            raise GatewayError("tool_disabled", "Tool is disabled by the imported service configuration")
        tool = next((t for t in self.config.get("tools", []) if t["name"] == name), None)
        if tool is None:
            raise GatewayError("tool_not_found", "REST tool not found")
        Draft202012Validator(tool.get("inputSchema", {"type": "object"})).validate(arguments)
        req = tool["request"]
        original = check_url(req["url"], template=True)
        url = substitute(req["url"], arguments, url=True)
        if check_url(url).netloc != original.netloc:
            raise ValueError("REST request cannot change configured host")
        headers = auth_headers(self.config) | substitute(req.get("headers", {}), arguments)
        kwargs = {"headers": {k: str(v) for k, v in headers.items()},
                  "params": substitute(req.get("query", {}), arguments)}
        if "json" in req:
            kwargs["json"] = substitute(req["json"], arguments)
        if "body" in req:
            body = substitute(req["body"], arguments)
            kwargs["content"] = body if isinstance(body, (str, bytes)) else json.dumps(body)
        options = tool.get("response", {})
        kind = options.get("type", "json")
        raw_budget = MAX_TOOL_RESPONSE_BYTES * 3 // 4 if kind in ("image", "audio") else MAX_TOOL_RESPONSE_BYTES
        async with self.client.stream(
            req.get("method", "GET"),
            url,
            **kwargs,
        ) as response:
            raw = await read_response_with_budget(response, max_bytes=raw_budget)
            status_code = response.status_code
            content_type = response.headers.get(
                "content-type",
                "application/octet-stream",
            )
            encoding = response.encoding or "utf-8"
        is_error = not 200 <= status_code < 300
        if kind in ("image", "audio"):
            return {
                "content": [{
                    "type": kind,
                    "data": base64.b64encode(raw).decode(),
                    "mimeType": content_type,
                }],
                "isError": is_error,
            }
        text_value = raw.decode(encoding, errors="replace")
        try:
            data = json.loads(raw) if kind != "text" else text_value
        except (ValueError, UnicodeDecodeError):
            data = text_value
        if not is_error:
            data = pointer(data, options.get("pointer"))
            if options.get("mapping"):
                data = {k: pointer(data, v) for k, v in options["mapping"].items()}
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if not isinstance(data, str):
            result["structuredContent"] = data if isinstance(data, dict) else {"data": data}
        return result


class SdkConnection(CapabilityConnection):
    def __init__(self, client, config=None, oauth_failure=None):
        self.client = client
        self.config = config or {}
        self.oauth_failure = oauth_failure

    async def _request(self, method, *args, **kwargs):
        if self.oauth_failure is None:
            return await getattr(self.client, method)(*args, **kwargs)
        if self.oauth_failure.done():
            raise self.oauth_failure.result()
        request = asyncio.create_task(getattr(self.client, method)(*args, **kwargs))
        try:
            await asyncio.wait({request, self.oauth_failure}, return_when=asyncio.FIRST_COMPLETED)
            if self.oauth_failure.done():
                raise self.oauth_failure.result()
            return await request
        finally:
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    def tool_allowed(self, name):
        return tool_allowed(self.config, name)

    async def _pages(self, method, key, budget):
        cursor, seen = None, set()
        while True:
            budget.before_page()
            result = wire(await self._request(method, cursor=cursor, cache_mode="bypass"))
            entries = result.get(key, [])
            if not isinstance(entries, list):
                raise GatewayError("invalid_pagination", "Capability page is not a list")
            await budget.consume(result, len(entries))
            yield entries
            cursor = result.get("nextCursor")
            if not cursor:
                return
            if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in seen:
                raise GatewayError("invalid_pagination", "Downstream returned an invalid or repeated capability cursor")
            seen.add(cursor)

    async def _list(self, method, key):
        found = []
        async for entries in self._pages(method, key, DiscoveryBudget.from_config(self.config)):
            found.extend(entries)
        return found

    async def iter_capabilities(self, *, budget):
        caps = wire(self.client.server_capabilities) or {}
        probes = []
        if "tools" in caps:
            probes.append(("tools", "list_tools", "tools"))
        if "resources" in caps:
            probes.extend((("resources", "list_resources", "resources"),
                           ("templates", "list_resource_templates", "resourceTemplates")))
        if "prompts" in caps:
            probes.append(("prompts", "list_prompts", "prompts"))
        if not probes:
            raise GatewayError("discovery_failed", "Downstream declared no discoverable capabilities")
        errors, succeeded = [], 0
        for capability, method, key in probes:
            try:
                async for entries in self._pages(method, key, budget):
                    if capability == "tools":
                        entries = [tool for tool in entries if self.tool_allowed(tool["name"])]
                    for offset in range(0, len(entries), budget.batch_size):
                        yield capability, entries[offset:offset + budget.batch_size]
                succeeded += 1
            except GatewayError as exc:
                from .runtime import oauth_http_401

                if oauth_http_401(exc) or exc.code == "discovery_budget_exceeded":
                    raise
                from .diagnostics import safe_diagnostic_error
                error = {"capability": capability, "error": safe_diagnostic_error(exc, config=self.config)["message"]}
                errors.append(error)
                yield "_error", [error]
            except Exception as exc:  # One unsupported capability does not hide the others.
                from .runtime import oauth_http_401

                if oauth_http_401(exc):
                    raise
                from .diagnostics import safe_diagnostic_error
                error = {"capability": capability, "error": safe_diagnostic_error(exc, config=self.config)["message"]}
                errors.append(error)
                yield "_error", [error]
        if not succeeded:
            summary = "; ".join(item["capability"] + ": " + item["error"] for item in errors)
            raise GatewayError("discovery_failed", "All declared MCP capabilities failed: " + summary)
        yield "_metadata", [{
            "capability_errors": errors, "serverInfo": wire(self.client.server_info) if self.client.server_info else {},
            "protocolVersion": self.client.protocol_version,
        }]

    async def discover(self):
        result = dict.fromkeys(("tools", "resources", "prompts", "templates"))
        result = {kind: [] for kind in result}
        async for kind, entries in iter_capabilities(self, budget=DiscoveryBudget.from_config(self.config)):
            if kind == "_error":
                for error in entries:
                    result[error["capability"]] = []
            elif kind == "_metadata":
                result.update(entries[0])
            else:
                result[kind].extend(entries)
        return result

    async def call(self, name, arguments):
        if not self.tool_allowed(name):
            raise GatewayError("tool_disabled", "Tool is disabled by the imported service configuration")
        return wire(await self._request("call_tool", name, arguments))

    async def read_resource(self, uri):
        return wire(await self._request("read_resource", uri))

    async def get_prompt(self, name, arguments):
        return wire(await self._request("get_prompt", name, arguments))


@asynccontextmanager
async def connect(spec: ServerSpec):
    config = connection_config(spec.config)
    validate_config(spec.transport, config)
    if spec.transport in PLUGINS:
        async with PLUGINS[spec.transport].connect(spec) as connection:
            yield PluginConnection(connection, config)
        return
    if spec.transport == "rest":
        async with httpx.AsyncClient(timeout=float(config.get("call_timeout", 60)),
                                     verify=config.get("verify_tls", True),
                                     follow_redirects=False, trust_env=False,
                                     proxy=config.get("proxy")) as http:
            yield RestConnection(config, http)
        return
    timeout = float(config.get("startup_timeout", 30))
    if spec.transport == "stdio":
        source = StdioServerParameters(command=config["command"], args=config.get("args", []),
                                       env=stdio_environment(config.get("env", {})), cwd=config.get("cwd"),
                                       encoding=config.get("encoding", "utf-8"))
        if sys.platform != "win32":
            from .posix_transport import posix_stdio
            source = posix_stdio(source, max_message_bytes=DiscoveryBudget.from_config(config).max_message_bytes)
        client = Client(source, read_timeout_seconds=float(config.get("call_timeout", 60)), cache=None)
        # The timeout scope encloses the SDK's scopes; disable its deadline once ready.
        with anyio.fail_after(timeout) as startup:
            async with client:
                startup.deadline = float("inf")
                yield SdkConnection(client, config)
    else:
        oauth_failure = asyncio.get_running_loop().create_future()

        async def oauth_response(response):
            if response.status_code == 401:
                error = GatewayError("auth_required", "授权已失效，请重新授权；本次不会自动重放调用")
                error.oauth_http_401 = True
                if not oauth_failure.done():
                    oauth_failure.set_result(error)
                raise error
        hooks = {"response": [oauth_response]} if config.get("auth", {}).get("type") == "oauth" else {}
        async with httpx2.AsyncClient(headers=auth_headers(config), event_hooks=hooks,
                                      timeout=float(config.get("call_timeout", 60)),
                                      verify=config.get("verify_tls", True),
                                      follow_redirects=False, trust_env=False) as http:
            def sse_http_factory(headers=None, timeout=None, auth=None):
                return httpx2.AsyncClient(headers=headers, timeout=timeout, auth=auth, event_hooks=hooks,
                    verify=config.get("verify_tls", True), follow_redirects=False, trust_env=False)
            source = (sse_client(config["url"], headers=auth_headers(config),
                                 timeout=timeout, sse_read_timeout=float(config.get("call_timeout", 60)),
                                 httpx_client_factory=sse_http_factory)
                      if spec.transport == "sse" else
                      streamable_http_client(config["url"], http_client=http))
            client = Client(source, read_timeout_seconds=float(config.get("call_timeout", 60)), cache=None)
            try:
                with anyio.fail_after(timeout) as startup:
                    async with client:
                        startup.deadline = float("inf")
                        yield SdkConnection(client, config, oauth_failure if hooks else None)
            except Exception:
                if oauth_failure.done():
                    raise oauth_failure.result() from None
                raise
