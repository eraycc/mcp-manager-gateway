"""Transport plugins normalize wire data without flattening MCP content."""
from __future__ import annotations

import base64
import copy
import json
import re
import sys
from contextlib import asynccontextmanager
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

from .runtime import GatewayError, ServerSpec
from .environment import connection_config, environment_value, stdio_environment


BUILTIN_TRANSPORTS = {"stdio", "streamable-http", "sse", "rest"}
PLUGINS = {}


def register_transport(name, plugin):
    """Plugin exposes validate(config) and async-context-manager connect(spec)."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", name) or name in BUILTIN_TRANSPORTS or name in PLUGINS:
        raise ValueError("Transport plugin name is invalid or already registered")
    if not callable(getattr(plugin, "validate", None)) or not callable(getattr(plugin, "connect", None)):
        raise ValueError("Plugin requires validate and connect")
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
        for piece in key.split("."):
            current = current[piece]
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
        raise ValueError("MCP config must be an object")
    if not isinstance(config.get("auth", {}), dict):
        raise ValueError("auth must be an object")
    for key in ("headers", "env", "env_headers"):
        value = config.get(key, {})
        if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                              for k, v in value.items()):
            raise ValueError(key + " must be a string-to-string object")
    if config.get("auth", {}).get("type", "none") not in {"none", "bearer", "basic", "api_key", "oauth"}:
        raise ValueError("Unsupported authentication type")
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
        tools = config.get("tools", [])
        if not isinstance(tools, list) or not tools:
            raise ValueError("REST requires at least one tool")
        names = set()
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise ValueError("Every REST tool requires a string name")
            if not tool.get("name") or tool["name"] in names:
                raise ValueError("REST tool names must be nonempty and unique")
            names.add(tool["name"])
            Draft202012Validator.check_schema(tool.get("inputSchema", {"type": "object"}))
            request = tool.get("request", {})
            if not isinstance(request, dict) or not isinstance(tool.get("response", {}), dict):
                raise ValueError("REST request and response must be objects")
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


def check_url(url, *, template=False):
    if not isinstance(url, str):
        raise ValueError("URL must be a string")
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


class RestConnection:
    def __init__(self, config, client):
        self.config = config
        self.client = client

    async def discover(self):
        return {"tools": [{k: v for k, v in tool.items() if k in (
            "name", "description", "inputSchema", "outputSchema", "annotations", "title")}
            | {"inputSchema": tool.get("inputSchema", {"type": "object"})}
            for tool in self.config.get("tools", []) if tool_allowed(self.config, tool["name"])],
            "resources": [], "prompts": [], "templates": []}

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
        response = await self.client.request(req.get("method", "GET"), url, **kwargs)
        options = tool.get("response", {})
        is_error = not 200 <= response.status_code < 300
        kind = options.get("type", "json")
        if kind in ("image", "audio"):
            return {"content": [{"type": kind, "data": base64.b64encode(response.content).decode(),
                                 "mimeType": response.headers.get("content-type", "application/octet-stream")}],
                    "isError": is_error}
        try:
            data = response.json() if kind != "text" else response.text
        except ValueError:
            data = response.text
        if not is_error:
            data = pointer(data, options.get("pointer"))
            if options.get("mapping"):
                data = {k: pointer(data, v) for k, v in options["mapping"].items()}
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if not isinstance(data, str):
            result["structuredContent"] = data if isinstance(data, dict) else {"data": data}
        return result


class SdkConnection:
    def __init__(self, client, config=None):
        self.client = client
        self.config = config or {}

    def tool_allowed(self, name):
        return tool_allowed(self.config, name)

    async def _list(self, method, key):
        cursor = None
        found = []
        seen = set()
        while True:
            result = wire(await getattr(self.client, method)(cursor=cursor, cache_mode="bypass"))
            found.extend(result.get(key, []))
            cursor = result.get("nextCursor")
            if not cursor:
                return found
            if cursor in seen:
                raise GatewayError("invalid_pagination", "Downstream repeated a capability cursor")
            seen.add(cursor)

    async def discover(self):
        caps = wire(self.client.server_capabilities) or {}
        result = {"tools": [], "resources": [], "prompts": [], "templates": []}
        if "tools" in caps:
            result["tools"] = [tool for tool in await self._list("list_tools", "tools")
                               if self.tool_allowed(tool["name"])]
        if "resources" in caps:
            result["resources"] = await self._list("list_resources", "resources")
            result["templates"] = await self._list("list_resource_templates", "resourceTemplates")
        if "prompts" in caps:
            result["prompts"] = await self._list("list_prompts", "prompts")
        result["serverInfo"] = wire(self.client.server_info) if self.client.server_info else {}
        result["protocolVersion"] = self.client.protocol_version
        return result

    async def call(self, name, arguments):
        if not self.tool_allowed(name):
            raise GatewayError("tool_disabled", "Tool is disabled by the imported service configuration")
        return wire(await self.client.call_tool(name, arguments))

    async def read_resource(self, uri):
        return wire(await self.client.read_resource(uri))

    async def get_prompt(self, name, arguments):
        return wire(await self.client.get_prompt(name, arguments))


@asynccontextmanager
async def connect(spec: ServerSpec):
    config = connection_config(spec.config)
    validate_config(spec.transport, config)
    if spec.transport in PLUGINS:
        async with PLUGINS[spec.transport].connect(spec) as connection:
            yield connection
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
        if sys.platform == "linux":
            from .posix_transport import posix_stdio
            source = posix_stdio(source)
        client = Client(source, read_timeout_seconds=float(config.get("call_timeout", 60)), cache=None)
        # The timeout scope encloses the SDK's scopes; disable its deadline once ready.
        with anyio.fail_after(timeout) as startup:
            async with client:
                startup.deadline = float("inf")
                yield SdkConnection(client, config)
    else:
        async with httpx2.AsyncClient(headers=auth_headers(config),
                                      timeout=float(config.get("call_timeout", 60)),
                                      verify=config.get("verify_tls", True),
                                      follow_redirects=False, trust_env=False) as http:
            def sse_http_factory(headers=None, timeout=None, auth=None):
                return httpx2.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                    verify=config.get("verify_tls", True), follow_redirects=False, trust_env=False)
            source = (sse_client(config["url"], headers=auth_headers(config),
                                 timeout=timeout, sse_read_timeout=float(config.get("call_timeout", 60)),
                                 httpx_client_factory=sse_http_factory)
                      if spec.transport == "sse" else
                      streamable_http_client(config["url"], http_client=http))
            client = Client(source, read_timeout_seconds=float(config.get("call_timeout", 60)), cache=None)
            with anyio.fail_after(timeout) as startup:
                async with client:
                    startup.deadline = float("inf")
                    yield SdkConnection(client, config)
