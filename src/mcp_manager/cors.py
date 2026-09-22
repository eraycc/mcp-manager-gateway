"""Dynamic browser and Host access policy for gateway endpoints."""
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse


def validate_origins(origins):
    if not isinstance(origins, list) or any(not isinstance(x, str) for x in origins):
        raise ValueError("cors_origins must be an array of origins")
    if "*" in origins and origins != ["*"]:
        raise ValueError("Use either * or explicit origins")
    for origin in origins:
        if origin == "*":
            continue
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("CORS origins must use http(s)://host[:port] without a path")
    return origins


def _host_parts(value):
    if not isinstance(value, str):
        raise TypeError("Host entries must be strings")
    value = value.strip().lower()
    if not value or any(char.isspace() for char in value) or any(char in value for char in "/?#@"):
        raise ValueError("Host entries must use host, IP, or host:port without a scheme or path")
    if value in {"*", "0.0.0.0"}:
        return value, None
    if value.startswith("["):
        closing = value.find("]")
        if closing <= 1:
            raise ValueError("Invalid IPv6 Host entry")
        host = value[1:closing]
        suffix = value[closing + 1:]
        if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
            raise ValueError("Invalid Host port")
        port = int(suffix[1:]) if suffix else None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Invalid Host port")
        return host, port
    if value.count(":") > 1:
        return value, None
    host, separator, raw_port = value.rpartition(":")
    if separator:
        if not host or not raw_port.isdigit():
            raise ValueError("Invalid Host port")
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError("Invalid Host port")
        return host, port
    return value, None


def validate_hosts(hosts):
    if not isinstance(hosts, list) or not hosts or len(hosts) > 200:
        raise ValueError("allowed_hosts must contain between 1 and 200 Host or IP entries")
    normalized = []
    for value in hosts:
        host, port = _host_parts(value)
        item = f"[{host}]:{port}" if ":" in host and port else (
            host if port is None else f"{host}:{port}"
        )
        if item not in normalized:
            normalized.append(item)
    return normalized


def host_allowed(current, allowed):
    try:
        current_host, current_port = _host_parts(current)
        configured = (
            allowed
            if isinstance(allowed, tuple)
            and all(isinstance(item, tuple) and len(item) == 2 for item in allowed)
            else tuple(_host_parts(value) for value in validate_hosts(allowed))
        )
    except (TypeError, ValueError):
        return False
    if any(host in {"*", "0.0.0.0"} for host, _port in configured):
        return True
    return any(host == current_host and (port is None or port == current_port)
               for host, port in configured)


class GatewayCORSMiddleware:
    def __init__(self, app, root_app):
        self.app, self.root_app = app, root_app
        self._host_source = None
        self._validated_hosts = None

    def validated_hosts(self):
        source = tuple(getattr(self.root_app.state, "allowed_hosts", ["*"]))
        if source != self._host_source:
            normalized = validate_hosts(list(source))
            self._host_source = source
            self._validated_hosts = tuple(_host_parts(value) for value in normalized)
        return self._validated_hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (
                scope["path"].rstrip("/") == "/mcp" or scope["path"].startswith("/gateway/v1/")):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        current_host = headers.get("host", "")
        allowed_hosts = getattr(self.root_app.state, "allowed_hosts", ["*"])
        try:
            configured_hosts = self.validated_hosts()
        except (TypeError, ValueError):
            configured_hosts = ()
        if not host_allowed(current_host, configured_hosts):
            return await JSONResponse({
                "detail": "Host is not allowed",
                "host": current_host,
                "allowed_hosts": allowed_hosts,
            }, status_code=403)(scope, receive, send)

        origins = getattr(self.root_app.state, "cors_origins", ["*"])
        origin = headers.get("origin")
        allowed = origins if "*" in origins else list(dict.fromkeys(
            origins + [self.root_app.state.config.public_url.rstrip("/")]))
        if origin and "*" not in allowed and origin not in allowed:
            return await JSONResponse({"detail": "Origin is not allowed"}, status_code=403)(scope, receive, send)

        async def validated(scope, receive, send):
            # The SDK only supports literal origins. This outer middleware has
            # validated the dynamic policy; keep body checks in the SDK.
            if origin and scope["path"].rstrip("/") == "/mcp":
                scope = dict(scope, headers=[(k, v) for k, v in scope["headers"] if k.lower() != b"origin"])
            await self.app(scope, receive, send)

        cors = CORSMiddleware(validated, allow_origins=allowed, allow_credentials=False,
                              allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                              allow_headers=["Authorization", "Content-Type", "Mcp-Session-Id",
                                             "Mcp-Protocol-Version", "Last-Event-ID",
                                             "X-MCP-Manager-Lease", "X-MCP-Manager-Client"],
                              expose_headers=["Mcp-Session-Id", "Mcp-Protocol-Version",
                                              "X-MCP-Manager-Client"])
        await cors(scope, receive, send)
