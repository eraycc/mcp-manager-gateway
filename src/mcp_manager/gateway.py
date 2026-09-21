"""Authenticated MCP wire endpoint and lease extension shared by the stdio bridge."""
import hashlib
import json
import secrets
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from jsonschema import ValidationError
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select
from starlette.responses import JSONResponse

from .about import NAME, VERSION
from .catalog import alias
from .database import ApiToken, User
from .discovery import DISCOVERY, Discovery, result_json, validate_meta, validation_error
from .identity import authenticate_token, expired
from .proposals import list_proposals, resource_index, submit_proposals
from .runtime import GatewayError

router = APIRouter(prefix="/gateway/v1")

RESOURCE_TOOLS = [
    {
        "name": "gateway_list_resources",
        "description": (
            "List authorized MCP resources, prompts and resource templates as JSON. "
            "Use only when the client does not support native MCP resource and prompt protocols. "
            "Filter by mcp and keyword; each result identifies its provider and how readable resources are read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mcp": {"type": "string", "maxLength": 2000, "default": ""},
                "keyword": {"type": "string", "maxLength": 2000, "default": ""},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "gateway_read_resource",
        "description": (
            "Read one authorized resource by the exact URI returned by gateway_list_resources. "
            "Use only when the client does not support the native MCP resources/read protocol."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "minLength": 1, "maxLength": 8192},
            },
            "required": ["uri"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
]
PROPOSAL_TOOL = {
    "name": "gateway_propose_mcp",
    "description": (
        "Submit one MCP configuration or a proposals array for administrator approval. "
        "Convert configurations to the documented standard JSON first. Valid proposals return approval IDs; "
        "mode, instance isolation and OAuth config isolation are set only by an approver."
    ),
    "inputSchema": {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "required": ["name", "transport", "config"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "slug": {"type": "string", "minLength": 1, "maxLength": 64},
                    "description": {"type": "string", "maxLength": 4000},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "transport": {"enum": ["stdio", "streamable-http", "http", "sse", "rest"]},
                    "config": {"type": "object"},
                    "source": {"type": "string", "maxLength": 128},
                    "purpose": {"type": "string", "maxLength": 4000},
                    "declared_capabilities": {"type": "array", "items": {"type": "string"}},
                    "requested_permissions": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["proposals"],
                "properties": {
                    "proposals": {
                        "type": "array", "minItems": 1, "maxItems": 100,
                        "items": {"type": "object"},
                    }
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {"const": "list"},
                    "q": {"type": "string", "maxLength": 200},
                    "status": {"enum": ["", "pending", "incomplete", "approved", "rejected"]},
                    "page": {"type": "integer", "minimum": 1, "default": 1},
                    "page_size": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                },
                "additionalProperties": False,
            },
        ]
    },
}


def discovery_tools(token):
    tools = list(DISCOVERY)
    if token and token.enable_resource_tools:
        tools.extend(RESOURCE_TOOLS)
    if token and token.enable_mcp_proposals:
        tools.append(PROPOSAL_TOOL)
    return tools




def owner(user, token, request=None):
    if user:
        return user.id, token.id if token else ""
    client = request.headers.get("X-MCP-Manager-Client", "") if request else ""
    if client:
        return "anonymous", hashlib.sha256(client.encode()).hexdigest()
    session = request.headers.get("Mcp-Session-Id", "") if request else ""
    return "anonymous", "session:" + session if session else secrets.token_urlsafe(24)


@router.post("/leases")
async def create_lease(request: Request):
    user, token, _ = await authenticate_token(request)
    client_secret = secrets.token_urlsafe(32) if user is None else None
    identity = owner(user, token, request) if user else (
        "anonymous", hashlib.sha256(client_secret.encode()).hexdigest())
    lease = request.app.state.runtime.create_lease(*identity, ttl=90, kind="bridge")
    result = {"id": lease.id, "heartbeat_seconds": 30, "expires_in": 90}
    if client_secret:
        result["client_secret"] = client_secret
    return result


@router.post("/leases/{lease_id}/heartbeat")
async def heartbeat(lease_id: str, request: Request):
    user, token, _ = await authenticate_token(request)
    request.app.state.runtime.heartbeat(lease_id, *owner(user, token, request))
    return {"ok": True}


@router.delete("/leases/{lease_id}")
async def release(lease_id: str, request: Request):
    user, token, _ = await authenticate_token(request)
    runtime = request.app.state.runtime
    lease = runtime.leases.get(lease_id)
    if lease and (lease.user_id, lease.token_id) != owner(user, token, request):
        raise HTTPException(403, "Lease belongs to a different caller")
    await runtime.release(lease_id)
    gateway = request.app.state.gateway
    for sid, bound in list(gateway.sessions.items()):
        if bound.get("explicit_lease") == lease_id:
            if bound["owner"] != owner(user, token, request):
                raise HTTPException(403, "Lease belongs to a different caller")
            await gateway.close_session(sid)
    return {"ok": True}


class Gateway:
    def __init__(self, app):
        self.app = app
        self.sessions = {}
        self.retention = {}
        self.discovery = Discovery(self)

    async def close_session(self, sid):
        bound = self.sessions.pop(sid, None)
        if bound:
            await self.app.state.runtime.release(bound["lease"])
        protocol = getattr(self.app.state, "protocol", None)
        if protocol:
            # Use the SDK public ASGI interface so its transport/task maps are
            # cleaned too; this is an internal revocation, not an authenticated call.
            transport = httpx.ASGITransport(app=protocol.session_manager.handle_request)
            async with httpx.AsyncClient(transport=transport, base_url=self.app.state.config.public_url) as client:
                response = await client.delete("/mcp", headers={"Mcp-Session-Id": sid})
                if response.status_code not in {200, 204, 404}:
                    response.raise_for_status()

    async def revoke(self, *, token_id):
        for sid, bound in list(self.sessions.items()):
            if bound["owner"][1] == token_id:
                await self.close_session(sid)
        for key, lease_id in list(self.retention.items()):
            if key[1] == token_id:
                self.retention.pop(key, None)
                await self.app.state.runtime.release(lease_id)

    async def reap(self):
        runtime = self.app.state.runtime
        idle = runtime.idle_seconds
        sessions = list(self.sessions.items())
        retention = list(self.retention.items())
        identities = {bound["owner"] for _, bound in sessions} | {identity for identity, _ in retention}
        token_ids = {identity[1] for identity in identities if identity[0] != "anonymous"}
        valid = set()
        if token_ids:
            async with self.app.state.db.session() as session:
                rows = await session.execute(select(ApiToken, User).join(User, User.id == ApiToken.user_id)
                                             .where(ApiToken.id.in_(token_ids)))
                valid = {(user.id, token.id) for token, user in rows
                         if not user.disabled and not token.disabled and not expired(token.expires_at)}
        # Validate only the snapshot included in the query. A connection can
        # arrive while the database yields and must be checked on the next pass.
        for sid, bound in sessions:
            if (self.sessions.get(sid) is bound and bound["owner"][0] != "anonymous"
                    and bound["owner"] not in valid):
                await self.close_session(sid)
        for identity, lease_id in retention:
            if (self.retention.get(identity) == lease_id and identity[0] != "anonymous"
                    and identity not in valid):
                self.retention.pop(identity, None)
                await runtime.release(lease_id)
        active = {lease_id for instance in runtime.instances.values() if instance.in_flight
                  for lease_id in instance.refs}
        for sid, bound in list(self.sessions.items()):
            if (idle > 0 and bound["lease"] not in active and bound.get("explicit_lease") not in active
                    and time.monotonic() - bound.get("touched", 0) >= idle):
                await self.close_session(sid)
        for key, lease_id in list(self.retention.items()):
            lease = runtime.leases.get(lease_id)
            if lease and idle > 0 and lease_id not in active and runtime.clock() - lease.touched >= idle:
                await runtime.release(lease_id)
            if lease_id not in runtime.leases:
                self.retention.pop(key, None)

    async def principal(self, request):
        return await authenticate_token(request)

    async def directory(self, request):
        user, token, allowed = await self.principal(request)
        cat = self.app.state.catalog
        rows = await cat.rows(ids=allowed)
        entries = []
        user_id = user.id if user else None

        async def authorize(server_id):
            current_user, current_token, current_allowed = await self.principal(request)
            if owner(current_user, current_token, request) != owner(user, token, request):
                raise GatewayError("permission_revoked", "Gateway identity changed")
            if server_id not in current_allowed:
                raise GatewayError("permission_revoked", "MCP access revoked")

        for row in rows:
            await cat.ensure_ready(
                row,
                user_id,
                authorize=lambda row_id=row.id: authorize(row_id),
            )
            for tool in cat.cached(row, user_id)["tools"]:
                entries.append((row, tool, dict(tool, name=alias(row.slug, tool["name"]))))
        return user, token, rows, entries

    def failed_target(self, rows, name, user_id):
        catalog = self.app.state.catalog
        for row in rows:
            error = catalog.failed_error(row, user_id)
            if error is None:
                continue
            if name in catalog.cached(row, user_id)["failed_gateway_names"]:
                return error
        return None

    def lease(self, request, user, token):
        runtime = self.app.state.runtime
        identity = owner(user, token, request)
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
                lease.touched = runtime.clock()
                return lease
            lease = runtime.create_lease(*identity, kind="retention")
            bound["lease"] = lease.id
            return lease
        key = identity
        lease = runtime.leases.get(self.retention.get(key, ""))
        if not lease:
            lease = runtime.create_lease(*identity, kind="retention")
            self.retention[key] = lease.id
        lease.touched = runtime.clock()
        return lease

    async def list_tools(self, ctx, params):
        _, token, _ = await self.principal(ctx.request)
        if token and token.discovery_mode == "discovery":
            return types.ListToolsResult(tools=[types.Tool.model_validate(x) for x in discovery_tools(token)])
        _user, _token, _rows, entries = await self.directory(ctx.request)
        tools = [e[2] for e in entries]
        return types.ListToolsResult(tools=[types.Tool.model_validate(x) for x in tools])

    async def call_tool(self, ctx, params):
        try:
            return await self._call_tool(ctx, params)
        except Exception as exc:
            from jsonschema import ValidationError
            if not isinstance(exc, (GatewayError, ValueError, HTTPException, ValidationError)):
                raise
            message = exc.message if isinstance(exc, ValidationError) else str(getattr(exc, "detail", exc))
            error = {"code": getattr(exc, "code", "invalid_request"), "message": message}
            if hasattr(exc, "details"):
                return result_json({"error": error | exc.details}, error=True)
            return types.CallToolResult(isError=True,
                content=[types.TextContent(type="text", text=message)], structuredContent={"error": error})

    async def _call_tool(self, ctx, params):
        request = ctx.request
        user, token, _ = await self.principal(request)
        name, arguments = params.name, params.arguments or {}
        discovery = token and token.discovery_mode == "discovery"
        if discovery and name == "gateway_propose_mcp":
            if isinstance(arguments, dict) and arguments.get("action") == "list":
                return result_json(await list_proposals(
                    self.app, user,
                    q=str(arguments.get("q", "")),
                    status=str(arguments.get("status", "")),
                    page=int(arguments.get("page", 1)),
                    page_size=int(arguments.get("page_size", 20)),
                ))
            return result_json(await submit_proposals(self.app, user, token, arguments))
        user, token, rows, entries = await self.directory(request)
        if discovery and token.enable_resource_tools and name == "gateway_list_resources":
            if not isinstance(arguments, dict) or set(arguments) - {"mcp", "keyword"}:
                raise ValueError("gateway_list_resources accepts only mcp and keyword")
            mcp_query = str(arguments.get("mcp", ""))
            keyword = str(arguments.get("keyword", "")).strip()
            cached = lambda row: self.app.state.catalog.cached(row, user.id if user else None)
            items = resource_index(rows, cached, mcp=mcp_query)
            search = {"mode": "keyword", "semantic_status": "disabled"}
            if keyword:
                literal = resource_index(rows, cached, mcp=mcp_query, keyword=keyword)
                literal_keys = {
                    json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
                    for item in literal
                }
                documents = []
                keyed = {}
                for index, item in enumerate(items):
                    document_id = hashlib.sha256(
                        json.dumps([item.get("mcp", {}).get("id"), item.get("kind"),
                                    item.get("uri") or item.get("uriTemplate") or item.get("name"), index],
                                   ensure_ascii=False).encode()
                    ).hexdigest()
                    keyed[document_id] = item
                    documents.append({"id": document_id, "text": json.dumps(item, ensure_ascii=False, default=str)})
                from .embedding_api import load_embedding_config
                config = await load_embedding_config(self.app.state)
                scope = hashlib.sha256(
                    json.dumps([user.id if user else "anonymous", token.id if token else "", "resources"]).encode()
                ).hexdigest()
                scores, search = await self.app.state.embeddings.scores(scope, documents, keyword, config)
                items = [
                    item for document_id, item in keyed.items()
                    if document_id in scores
                    or json.dumps(item, sort_keys=True, ensure_ascii=False, default=str) in literal_keys
                ]
                for document_id, item in keyed.items():
                    if document_id in scores and item in items:
                        item["semantic_score"] = scores[document_id]
            return result_json({"items": items, "count": len(items), "search": search})
        if discovery and token.enable_resource_tools and name == "gateway_read_resource":
            if not isinstance(arguments, dict) or set(arguments) != {"uri"}:
                raise ValueError("gateway_read_resource requires only uri")
            return await self.read_resource_tool(request, user, token, rows, str(arguments["uri"]))
        if discovery and name in {"gateway_search_mcps", "gateway_search_tools"}:
            return await self.discovery.search(name, arguments, request)
        if discovery and name == "gateway_search":
            q = arguments.get("query", "").strip().lower()
            q = "" if q == "*" else q
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
            arguments = validate_meta("gateway_call", arguments)
            name, arguments = arguments["name"], arguments["arguments"]
        entry = next((e for e in entries if e[2]["name"] == name), None)
        if not entry:
            failure = self.failed_target(rows, name, user.id if user else None)
            if failure is not None:
                raise failure
            raise ValueError("Tool not found in authorized cache; ask an administrator to refresh")
        row, tool, _ = entry

        async def authorize():
            _fresh_user, _fresh_token, allowed = await self.principal(request)
            fresh = await self.app.state.catalog.get(row.id)
            if row.id not in allowed or fresh.revision != row.revision:
                raise GatewayError("permission_revoked", "MCP permission or configuration changed")

        try:
            result = await self.app.state.catalog.call(row, self.lease(request, user, token), tool["name"],
                                                       arguments, user=user, token=token, authorize=authorize)
        except ValidationError as exc:
            if discovery:
                raise validation_error(exc, name=name) from None
            raise
        return types.CallToolResult.model_validate(result)

    async def read_resource_tool(self, request, user, token, rows, uri):
        row = next((r for r in rows if uri.startswith("mcp-manager://" + r.id + "/")), None)
        if not row:
            raise ValueError("Resource not authorized")
        original = uri[len("mcp-manager://" + row.id + "/"):]
        result = await self.app.state.runtime.perform(
            await self.app.state.catalog.spec(row, user.id if user else None),
            self.lease(request, user, token).id,
            "read_resource",
            original,
            authorize=lambda: self.ensure(request, row),
        )
        for content in result.get("contents", []):
            content["uri"] = "mcp-manager://" + row.id + "/" + content["uri"]
        return result_json({
            "mcp": {"id": row.id, "name": row.name, "slug": row.slug},
            "contents": result.get("contents", []),
        })

    async def list_resources(self, ctx, params):
        user, _token, rows, _ = await self.directory(ctx.request)
        items = []
        for row in rows:
            for item in self.app.state.catalog.cached(row, user.id if user else None)["resources"]:
                items.append(types.Resource.model_validate(dict(item, uri="mcp-manager://" + row.id + "/" + item["uri"])))
        return types.ListResourcesResult(resources=items)

    async def list_templates(self, ctx, params):
        user, _token, rows, _ = await self.directory(ctx.request)
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
            authorize=lambda current=row: self.ensure(ctx.request, current))
        for content in result.get("contents", []):
            content["uri"] = "mcp-manager://" + row.id + "/" + content["uri"]
        return types.ReadResourceResult.model_validate(result)

    async def ensure(self, request, row):
        _, _, allowed = await self.principal(request)
        fresh = await self.app.state.catalog.get(row.id)
        if row.id not in allowed or fresh.revision != row.revision:
            raise GatewayError("permission_revoked", "MCP access revoked")

    async def list_prompts(self, ctx, params):
        user, _token, rows, _ = await self.directory(ctx.request)
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
                        authorize=lambda current=row: self.ensure(ctx.request, current))
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
            identity = owner(user, token, request)
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
                bound["touched"] = time.monotonic()
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
                    session_owner = identity
                    if user is None and not request.headers.get("X-MCP-Manager-Client"):
                        session_owner = ("anonymous", "session:" + sid.decode())
                        lease.user_id, lease.token_id = session_owner
                    self.gateway.sessions[sid.decode()] = {
                        "owner": session_owner, "lease": lease.id, "explicit_lease": explicit,
                        "touched": time.monotonic()}
                if request.method == "DELETE" and session and message["status"] < 300:
                    bound = self.gateway.sessions.pop(session, None)
                    if bound:
                        await self.root_app.state.runtime.release(bound["lease"])
            await send(message)
        await self.app(scope, receive, capture)


def install_gateway(app):
    gateway = Gateway(app)
    protocol = Server(NAME, version=VERSION, on_list_tools=gateway.list_tools,
                      on_call_tool=gateway.call_tool, on_list_resources=gateway.list_resources,
                      on_list_resource_templates=gateway.list_templates, on_read_resource=gateway.read_resource,
                      on_list_prompts=gateway.list_prompts, on_get_prompt=gateway.get_prompt)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    endpoint = protocol.streamable_http_app(streamable_http_path="/mcp", session_idle_timeout=None,
                                             transport_security=security)
    app.router.routes.extend(endpoint.router.routes)
    app.include_router(router)
    app.add_middleware(MCPAuthMiddleware, root_app=app, gateway=gateway)
    app.state.protocol, app.state.gateway = protocol, gateway
