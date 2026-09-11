"""Authenticated MCP wire endpoint and lease extension shared by the stdio bridge."""
import json
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from .catalog import alias
from .identity import authenticate_token
from .runtime import GatewayError

router = APIRouter(prefix="/gateway/v1")
DISCOVERY = [
    {"name": "gateway_search", "description": "Search authorized cached MCP tools without starting servers.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "additionalProperties": False}},
    {"name": "gateway_inspect", "description": "Read the full cached schema for a discovered tool.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "gateway_call", "description": "Call a discovered MCP tool by its exact gateway name.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
                     "required": ["name"]}},
]


def owner(user, token):
    return user.id if user else "anonymous", token.id if token else ""


@router.post("/leases")
async def create_lease(request: Request):
    user, token, _ = await authenticate_token(request)
    lease = request.app.state.runtime.create_lease(*owner(user, token), ttl=90, kind="bridge")
    return {"id": lease.id, "heartbeat_seconds": 30, "expires_in": 90}


@router.post("/leases/{lease_id}/heartbeat")
async def heartbeat(lease_id: str, request: Request):
    user, token, _ = await authenticate_token(request)
    request.app.state.runtime.heartbeat(lease_id, *owner(user, token))
    return {"ok": True}


@router.delete("/leases/{lease_id}")
async def release(lease_id: str, request: Request):
    user, token, _ = await authenticate_token(request)
    runtime = request.app.state.runtime
    runtime.check_lease(lease_id, *owner(user, token))
    await runtime.release(lease_id)
    return {"ok": True}


class Gateway:
    def __init__(self, app):
        self.app = app
        self.sessions = {}
        self.retention = {}

    async def principal(self, request):
        return await authenticate_token(request)

    async def directory(self, request):
        user, token, allowed = await self.principal(request)
        cat = self.app.state.catalog
        rows = [r for r in await cat.rows() if r.id in allowed]
        entries = []
        for row in rows:
            for tool in cat.cached(row, user.id if user else None)["tools"]:
                entries.append((row, tool, dict(tool, name=alias(row.slug, tool["name"]))))
        return user, token, rows, entries

    def lease(self, request, user, token):
        runtime = self.app.state.runtime
        identity = owner(user, token)
        explicit = request.headers.get("X-MCP-Manager-Lease")
        if explicit:
            return runtime.check_lease(explicit, *identity)
        session = request.headers.get("Mcp-Session-Id")
        if session:
            bound = self.sessions.get(session)
            if not bound or bound["owner"] != identity:
                raise HTTPException(403, "MCP session belongs to another principal")
            lease = runtime.leases.get(bound["lease"])
            if lease:
                return lease
            lease = runtime.create_lease(*identity, kind="retention")
            bound["lease"] = lease.id
            return lease
        key = identity
        lease = runtime.leases.get(self.retention.get(key, ""))
        if not lease:
            lease = runtime.create_lease(*identity, kind="retention")
            self.retention[key] = lease.id
        return lease

    async def list_tools(self, ctx, params):
        user, token, rows, entries = await self.directory(ctx.request)
        tools = DISCOVERY if token and token.discovery_mode == "discovery" else [e[2] for e in entries]
        return types.ListToolsResult(tools=[types.Tool.model_validate(x) for x in tools])

    async def call_tool(self, ctx, params):
        try:
            return await self._call_tool(ctx, params)
        except Exception as exc:
            from jsonschema import ValidationError
            if not isinstance(exc, (GatewayError, ValueError, HTTPException, ValidationError)):
                raise
            message = exc.message if isinstance(exc, ValidationError) else str(getattr(exc, "detail", exc))
            return types.CallToolResult(isError=True,
                content=[types.TextContent(type="text", text=message)],
                structuredContent={"error": {"code": getattr(exc, "code", "invalid_request"), "message": message}})

    async def _call_tool(self, ctx, params):
        request = ctx.request
        user, token, rows, entries = await self.directory(request)
        name, arguments = params.name, params.arguments or {}
        discovery = token and token.discovery_mode == "discovery"
        if discovery and name == "gateway_search":
            q = arguments.get("query", "").lower()
            matches = [{"name": e[2]["name"], "description": e[1].get("description", ""), "mcp": e[0].name}
                       for e in entries if q in (e[2]["name"] + " " + e[1].get("description", "")).lower()]
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(matches, ensure_ascii=False))])
        if discovery and name == "gateway_inspect":
            tool = next((e[2] for e in entries if e[2]["name"] == arguments.get("name")), None)
            if tool is None:
                raise ValueError("Tool not found in authorized cache")
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(tool, ensure_ascii=False))])
        if discovery:
            if name != "gateway_call":
                raise ValueError("Use gateway_call in discovery mode")
            name, arguments = arguments.get("name"), arguments.get("arguments", {})
        entry = next((e for e in entries if e[2]["name"] == name), None)
        if not entry:
            raise ValueError("Tool not found in authorized cache; ask an administrator to refresh")
        row, tool, _ = entry

        async def authorize():
            fresh_user, fresh_token, allowed = await self.principal(request)
            fresh = await self.app.state.catalog.get(row.id)
            if row.id not in allowed or fresh.revision != row.revision:
                raise GatewayError("permission_revoked", "MCP permission or configuration changed")

        result = await self.app.state.catalog.call(row, self.lease(request, user, token), tool["name"],
                                                   arguments, user=user, token=token, authorize=authorize)
        return types.CallToolResult.model_validate(result)

    async def list_resources(self, ctx, params):
        user, token, rows, _ = await self.directory(ctx.request)
        items = []
        for row in rows:
            for item in self.app.state.catalog.cached(row, user.id if user else None)["resources"]:
                items.append(types.Resource.model_validate(dict(item, uri="mcp-manager://" + row.id + "/" + item["uri"])))
        return types.ListResourcesResult(resources=items)

    async def list_templates(self, ctx, params):
        user, token, rows, _ = await self.directory(ctx.request)
        items = []
        for row in rows:
            for item in self.app.state.catalog.cached(row, user.id if user else None)["templates"]:
                items.append(types.ResourceTemplate.model_validate(
                    dict(item, uriTemplate="mcp-manager://" + row.id + "/" + item["uriTemplate"])))
        return types.ListResourceTemplatesResult(resourceTemplates=items)

    async def read_resource(self, ctx, params):
        user, token, rows, _ = await self.directory(ctx.request)
        uri = str(params.uri)
        row = next((r for r in rows if uri.startswith("mcp-manager://" + r.id + "/")), None)
        if not row:
            raise ValueError("Resource not authorized")
        original = uri[len("mcp-manager://" + row.id + "/"):]
        result = await self.app.state.runtime.perform(await self.app.state.catalog.spec(row, user.id if user else None),
            self.lease(ctx.request, user, token).id, "read_resource", original,
            authorize=lambda: self.ensure(ctx.request, row))
        for content in result.get("contents", []):
            content["uri"] = "mcp-manager://" + row.id + "/" + content["uri"]
        return types.ReadResourceResult.model_validate(result)

    async def ensure(self, request, row):
        _, _, allowed = await self.principal(request)
        fresh = await self.app.state.catalog.get(row.id)
        if row.id not in allowed or fresh.revision != row.revision:
            raise GatewayError("permission_revoked", "MCP access revoked")

    async def list_prompts(self, ctx, params):
        user, token, rows, _ = await self.directory(ctx.request)
        prompts = [types.Prompt.model_validate(dict(p, name=alias(row.slug, p["name"])))
                   for row in rows for p in self.app.state.catalog.cached(row, user.id if user else None)["prompts"]]
        return types.ListPromptsResult(prompts=prompts)

    async def get_prompt(self, ctx, params):
        user, token, rows, _ = await self.directory(ctx.request)
        for row in rows:
            for prompt in self.app.state.catalog.cached(row, user.id if user else None)["prompts"]:
                if alias(row.slug, prompt["name"]) == params.name:
                    result = await self.app.state.runtime.perform(
                        await self.app.state.catalog.spec(row, user.id if user else None),
                        self.lease(ctx.request, user, token).id, "get_prompt", prompt["name"], params.arguments or {},
                        authorize=lambda: self.ensure(ctx.request, row))
                    return types.GetPromptResult.model_validate(result)
        raise ValueError("Prompt not authorized")


class MCPAuthMiddleware:
    def __init__(self, app, root_app, gateway):
        self.app, self.root_app, self.gateway = app, root_app, gateway

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") != "/mcp":
            return await self.app(scope, receive, send)
        scope["app"] = self.root_app
        request = Request(scope, receive)
        try:
            user, token, _ = await authenticate_token(request)
            identity = owner(user, token)
            explicit = request.headers.get("X-MCP-Manager-Lease")
            if explicit:
                self.root_app.state.runtime.check_lease(explicit, *identity)
            session = request.headers.get("Mcp-Session-Id")
            if session:
                bound = self.gateway.sessions.get(session)
                if not bound:
                    raise HTTPException(404, "Unknown MCP session; initialize again")
                if bound["owner"] != identity:
                    raise HTTPException(403, "MCP session belongs to another principal")
        except (HTTPException, GatewayError) as exc:
            response = JSONResponse({"error": {"code": -32001, "message": str(getattr(exc, "detail", exc))}},
                                    status_code=getattr(exc, "status_code", 403))
            return await response(scope, receive, send)

        async def capture(message):
            if message["type"] == "http.response.start":
                headers = {k.lower(): v for k, v in message.get("headers", [])}
                sid = headers.get(b"mcp-session-id")
                if sid and sid.decode() not in self.gateway.sessions:
                    lease = self.root_app.state.runtime.create_lease(*identity, kind="retention")
                    self.gateway.sessions[sid.decode()] = {"owner": identity, "lease": lease.id}
                if request.method == "DELETE" and session and message["status"] < 300:
                    bound = self.gateway.sessions.pop(session, None)
                    if bound:
                        await self.root_app.state.runtime.release(bound["lease"])
            await send(message)
        await self.app(scope, receive, capture)


def install_gateway(app):
    gateway = Gateway(app)
    protocol = Server("MCP Manager", version="0.1.0", on_list_tools=gateway.list_tools,
                      on_call_tool=gateway.call_tool, on_list_resources=gateway.list_resources,
                      on_list_resource_templates=gateway.list_templates, on_read_resource=gateway.read_resource,
                      on_list_prompts=gateway.list_prompts, on_get_prompt=gateway.get_prompt)
    public = urlsplit(app.state.config.public_url)
    security = TransportSecuritySettings(allowed_hosts=[public.netloc, "127.0.0.1:*", "localhost:*", "[::1]:*", "test"],
                                         allowed_origins=[app.state.config.public_url])
    endpoint = protocol.streamable_http_app(streamable_http_path="/mcp", session_idle_timeout=None,
                                             transport_security=security)
    app.router.routes.extend(endpoint.router.routes)
    app.include_router(router)
    app.add_middleware(MCPAuthMiddleware, root_app=app, gateway=gateway)
    app.state.protocol, app.state.gateway = protocol, gateway
