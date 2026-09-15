"""Progressive MCP discovery: service summaries, full tool contracts, exact execution."""
import copy
import json

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from mcp import types

from .runtime import GatewayError
from .tool_index import document, fingerprint, normalize, paginate, query_text, rank, schema_text

PAGING = {
    "limit": {"type": "integer", "minimum": 1, "maximum": 100,
              "description": "Page size (default 20). Follow next_cursor to get all matches."},
    "cursor": {"type": "string", "maxLength": 2048,
               "description": "Opaque next_cursor from the previous result; keep the query unchanged."},
}
DISCOVERY = [
    {"name": "gateway_search_mcps",
     "description": "Find authorized MCP services by name or purpose. Empty query or '*' lists ALL services "
                    "(follow next_cursor). Returns service summaries with name-only tools_list, never tool schemas. "
                    "Then call gateway_search_tools with an exact returned mcp id. "
                    'Example: {"query":"*"} or {"query":"file storage"}.',
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "maxLength": 2000, "default": "",
                   "description": "Service name, slug or purpose; empty/'*' lists all."},
         **PAGING, "limit": {**PAGING["limit"], "maximum": 200, "default": 50}},
         "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "destructiveHint": False}},
    {"name": "gateway_search_tools",
     "description": "Discover tools and their FULL original inputSchema before calling. "
                    "mcp is service id/slug/name/purpose; tool is tool name or task/parameter keywords. "
                    "Empty or '*' means all: mcp='files',tool='' lists that service's tools; "
                    "mcp='*',tool='read_file' searches all authorized services; both empty/'*' lists all "
                    "services and tools (follow next_cursor). Results include exact gateway_name, "
                    "MCP owner, descriptions, inputSchema and gateway_call examples/templates. "
                    "Read required fields/types, then call gateway_call using that exact name and arguments.",
     "inputSchema": {"type": "object", "properties": {
         "mcp": {"type": "string", "maxLength": 2000, "default": "",
                 "description": "Exact returned MCP id preferred; slug/name or fuzzy purpose allowed. Empty/'*'=all."},
         "tool": {"type": "string", "maxLength": 2000, "default": "",
                  "description": "Tool name, exact gateway_name, purpose or parameter keyword. Empty/'*'=all."},
         **PAGING}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "destructiveHint": False}},
    {"name": "gateway_call",
     "description": "Execute ONE discovered tool. First use gateway_search_tools to read its original inputSchema. "
                    "Copy the exact gateway_name into name; put ONLY that tool's parameters in arguments, "
                    "respecting required, types, enum and nested schemas. Never guess a tool name. "
                    'Example: {"name":"files__read_file","arguments":{"path":"example.txt"}}. '
                    "The example is illustrative; use the actual discovered schema.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "minLength": 1, "maxLength": 128,
                  "description": "Exact gateway_name from gateway_search_tools."},
         "arguments": {"type": "object", "description": "Arguments matching the discovered tool's ORIGINAL inputSchema."}},
         "required": ["name", "arguments"], "additionalProperties": False}},
]
SCHEMAS = {item["name"]: item["inputSchema"] for item in DISCOVERY}


class DiscoveryError(GatewayError):
    def __init__(self, code, message, **details):
        super().__init__(code, message)
        self.details = details


def validation_error(exc, *, name=None, code="invalid_arguments"):
    path = "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in exc.absolute_path)
    return DiscoveryError(code, exc.message, path=path if path != "/" else "",
                          expected=exc.validator_value, tool=name,
                          recovery="Read gateway_search_tools inputSchema for this exact tool, correct arguments, then retry.")


def validate_meta(name, args):
    candidate = args
    if name == "gateway_call" and isinstance(args, dict) and "arguments" not in args:
        candidate = {**args, "arguments": {}}  # existing clients may omit an empty argument object
    try:
        Draft202012Validator(SCHEMAS[name]).validate(candidate)
    except ValidationError as exc:
        raise validation_error(exc, name=name, code="invalid_request") from None
    return candidate


def result_json(value, *, error=False):
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
                                structuredContent=value, isError=error)


def service_description(configured, tools):
    if configured.strip():
        return configured, "configured"
    ordered = sorted(tools, key=lambda tool: tool.get("name", ""))
    descriptions = []
    for tool in ordered:
        value = tool.get("description")
        if isinstance(value, str) and value.strip() and value.strip() not in descriptions:
            descriptions.append(value.strip())
    if descriptions:
        summary = f"Provides {len(ordered)} tools: " + "; ".join(descriptions[:3])
        return summary[:480], "generated"
    names = [tool["name"] for tool in ordered if isinstance(tool.get("name"), str)]
    if names:
        suffix = " …" if len(names) > 6 else ""
        return (f"Provides {len(names)} tools: " + ", ".join(names[:6]) + suffix)[:480], "generated"
    return "", "missing"


def tool_contract(row, original, exposed):
    result = copy.deepcopy(exposed)
    result.update(gateway_name=exposed["name"], original_name=original["name"],
                  mcp_id=row.id, mcp_name=row.name, mcp_slug=row.slug)
    schema = original.get("inputSchema", {})
    examples = []
    try:
        validator = Draft202012Validator(schema)
        for example in schema.get("examples", []):
            if isinstance(example, dict) and validator.is_valid(example):
                examples.append({"name": exposed["name"], "arguments": copy.deepcopy(example)})
            if len(examples) == 3:
                break
    except (SchemaError, TypeError):
        pass
    result["examples"] = examples
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    result["invocation"] = {
        "tool": "gateway_call",
        "template": {"name": exposed["name"], "arguments": {
            key: "<supply " + str(properties[key].get("type", "value")
                                  if isinstance(properties.get(key), dict) else "value") + ">" for key in required}},
        "is_template": True,
        "instructions": "Replace placeholders using inputSchema. Examples satisfy the schema, not necessarily business rules.",
    }
    return result


class Discovery:
    def __init__(self, gateway):
        self.gateway = gateway

    async def snapshot(self, request):
        user, token, rows, entries = await self.gateway.directory(request)
        catalog = self.gateway.app.state.catalog
        summaries = []
        for row in rows:
            if row.mode == "disabled":
                continue
            cache = catalog.cached(row, user.id if user else None)
            service_tools = [original for service, original, _exposed in entries if service.id == row.id]
            tools_list = sorted(tool["name"] for tool in service_tools)
            description, description_source = service_description(row.description, service_tools)
            failure = catalog.failure_details(row, user.id if user else None)
            summaries.append({
                "id": row.id,
                "mcp": row.id,
                "name": row.name,
                "slug": row.slug,
                "description": description,
                "description_source": description_source,
                "tags": row.tags,
                "tool_count": len(tools_list),
                "tools_list": tools_list,
                "catalog_status": cache["cache_status"],
                "status": failure["status"],
                "startup_failure_count": failure["startup_failure_count"],
                "failure_reason": failure["failure_reason"],
                "last_startup_error": cache["last_startup_error"],
                "last_startup_error_code": cache["last_startup_error_code"],
                "last_startup_failure_at": cache["last_startup_failure_at"],
                "failure_scope": failure["failure_scope"],
                "revision": row.revision,
            })
        summaries.sort(key=lambda x: (x["slug"], x["id"]))
        visible = {s["id"] for s in summaries}
        entries = sorted((e for e in entries if e[0].id in visible), key=lambda e: (e[0].slug, e[2]["name"]))
        tools = [tool_contract(*entry) for entry in entries]
        for tool in tools:
            tool["catalog_status"] = "ready"
        # Scope includes user identity, token identity and visible content; private OAuth caches cannot cross.
        scope = fingerprint([user.id if user else "anonymous", token.id if token else "", summaries, tools])
        return summaries, tools, scope

    async def search(self, name, arguments, request):
        arguments = validate_meta(name, arguments)
        services, tools, scope = await self.snapshot(request)
        from .embedding_api import load_embedding_config
        state = self.gateway.app.state
        config = await load_embedding_config(state)
        user, token, _ = await self.gateway.principal(request)
        embedding_scope = fingerprint([user.id if user else "anonymous", token.id if token else ""])
        async def authorize_embeddings():
            _, _, latest_scope = await self.snapshot(request)
            if latest_scope != scope:
                raise DiscoveryError("catalog_changed", "Permissions or catalog changed; retry the search.")
        service_docs = [document(s["id"], [s["id"], s["slug"], s["name"]], [
            (s["name"] + " " + s["slug"], 5), (" ".join(s["tags"]), 3),
            (" ".join(s["tools_list"]), 2), (s["description"], 1)])
            for s in services]
        mcp_query = query_text(arguments.get("query" if name == "gateway_search_mcps" else "mcp", ""))
        matched, service_search = await rank(service_docs, mcp_query, state.embeddings, config,
                                             embedding_scope + ":mcps", authorize=authorize_embeddings)
        matching_ids = {doc["id"] for doc, _ in matched}
        service_by_id = {s["id"]: s for s in services}
        selected = [service_by_id[doc["id"]] | {"match": match} for doc, match in matched]
        search_status = {"services": service_search}
        if name == "gateway_search_mcps":
            items = selected
        else:
            selected_tools = [t for t in tools if t["mcp_id"] in matching_ids]
            docs = [document(t["gateway_name"], [t["gateway_name"], t["original_name"]], [
                (t["gateway_name"] + " " + t["original_name"] + " " + (t.get("title") or ""), 5),
                (t.get("description") or "", 2), (schema_text(t["inputSchema"]), 3),
                (t["mcp_name"] + " " + t["mcp_slug"], .5)]) for t in selected_tools]
            matched_tools, search_status["tools"] = await rank(
                docs, query_text(arguments.get("tool", "")), state.embeddings, config, embedding_scope + ":tools",
                authorize=authorize_embeddings)
            by_name = {t["gateway_name"]: t for t in selected_tools}
            items = [by_name[doc["id"]] | {"match": match} for doc, match in matched_tools]
        # Recheck permission/cache after slow external embedding requests before disclosing results.
        _, _, current_scope = await self.snapshot(request)
        if current_scope != scope:
            raise DiscoveryError("catalog_changed", "Permissions or catalog changed; retry the search.")
        query = {k: normalize(query_text(arguments.get(k, "")))
                 for k in ("query",) if name == "gateway_search_mcps"}
        if name != "gateway_search_mcps":
            query = {k: normalize(query_text(arguments.get(k, ""))) for k in ("mcp", "tool")}
        version = fingerprint([scope, name, query, fingerprint(config), items, search_status])
        response = paginate(items, cursor=arguments.get("cursor"), limit=arguments.get(
            "limit", 50 if name == "gateway_search_mcps" else 20), version=version, key=state.config.jwt_secret)
        response["search"] = search_status
        if name == "gateway_search_tools":
            response["mcps"] = selected
        response["next_step"] = ("Call gateway_search_tools with a returned mcp id." if name == "gateway_search_mcps"
                                 else "Read inputSchema; call gateway_call with exact gateway_name and valid arguments.")
        return result_json(response)
