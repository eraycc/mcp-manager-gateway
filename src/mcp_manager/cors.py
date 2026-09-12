"""Dynamic browser access for bearer-authenticated gateway endpoints only."""
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


class GatewayCORSMiddleware:
    def __init__(self, app, root_app):
        self.app, self.root_app = app, root_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (
                scope["path"].rstrip("/") == "/mcp" or scope["path"].startswith("/gateway/v1/")):
            return await self.app(scope, receive, send)
        origins = getattr(self.root_app.state, "cors_origins", ["*"])
        origin = Headers(scope=scope).get("origin")
        allowed = origins if "*" in origins else list(dict.fromkeys(
            origins + [self.root_app.state.config.public_url.rstrip("/")]))
        if origin and "*" not in allowed and origin not in allowed:
            return await JSONResponse({"detail": "Origin is not allowed"}, status_code=403)(scope, receive, send)

        async def validated(scope, receive, send):
            # The SDK only supports literal origins. This outer middleware has
            # validated the dynamic policy; keep its independent Host/body checks.
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
