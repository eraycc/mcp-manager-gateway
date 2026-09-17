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
     "description": "Disclose FULL original inputSchema only for tools needed before gateway_call. "
                    "Use tool for one exact name or natural-language purpose; use tools for several exact "
                    "original names/gateway_names. Omit both only to intentionally enumerate a service. "
                    "mcp scopes by service id/slug/name/purpose; empty or '*' searches all authorized services. "
                    "Follow next_cursor when truncated, then call gateway_call with an exact gateway_name.",
     "inputSchema": {"type": "object", "properties": {
         "mcp": {"type": "string", "maxLength": 2000, "default": "",
                 "description": "Exact returned MCP id preferred; slug/name or fuzzy purpose allowed. Empty/'*'=all."},
         "tool": {"type": "string", "maxLength": 2000, "default": "",
                  "description": "One tool name, gateway_name, purpose or parameter keyword. Empty/'*'=all."},
         "tools": {"type": "array",
                   "items": {"type": "string", "minLength": 1, "maxLength": 128},
                   "minItems": 1, "maxItems": 50, "uniqueItems": True,
                   "description": "Exact original tool names or gateway_names to disclose together."},
         **PAGING}, "not": {"required": ["tool", "tools"]}, "additionalProperties": False},
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


def schema_needs_template(value, *, nested=False):
    if isinstance(value, bool):
        return nested
    if not isinstance(value, dict):
        return False
    if any(key in value for key in (
            "anyOf", "oneOf", "allOf", "$ref", "if", "then", "else",
            "dependentSchemas", "dependentRequired")):
        return True
    if value.get("type") == "array" or (nested and value.get("type") == "object"):
        return True
    for key, child in value.items():
        if key in {"properties", "$defs", "definitions"} and isinstance(child, dict):
            if any(schema_needs_template(item, nested=True) for item in child.values()):
                return True
        elif key in {"items", "additionalProperties", "prefixItems"}:
            values = child if isinstance(child, list) else [child]
            if any(schema_needs_template(item, nested=True) for item in values):
                return True
    return False


def resolve_local_ref(root, ref):
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    value = root
    try:
        for piece in ref[2:].split("/"):
            value = value[piece.replace("~1", "/").replace("~0", "~")]
    except (KeyError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def merge_schema(base, branch, *, discard=frozenset()):
    merged = {key: copy.deepcopy(value) for key, value in base.items() if key not in discard}
    if branch is False:
        return None
    if branch is True:
        return merged
    if not isinstance(branch, dict):
        return None
    for key, value in branch.items():
        if key == "properties" and isinstance(value, dict):
            properties = copy.deepcopy(merged.get("properties", {}))
            for name, child in value.items():
                if isinstance(properties.get(name), dict) and isinstance(child, dict):
                    properties[name] = merge_schema(properties[name], child)
                else:
                    properties[name] = copy.deepcopy(child)
            merged["properties"] = properties
        elif key == "required" and isinstance(value, list):
            merged["required"] = list(dict.fromkeys([*merged.get("required", []), *value]))
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def template_value(schema, root, *, depth=0, refs=frozenset()):
    if not isinstance(schema, dict) or depth >= 8:
        return "<value>"
    ref = schema.get("$ref")
    if ref:
        if ref in refs:
            return "<value>"
        resolved = resolve_local_ref(root, ref)
        if resolved is None:
            return "<value>"
        return template_value(resolved, root, depth=depth + 1, refs=refs | {ref})
    branches = schema.get("allOf")
    if isinstance(branches, list) and branches:
        candidate = merge_schema(schema, True, discard={"allOf"})
        for branch in branches:
            candidate = merge_schema(candidate, branch)
            if candidate is None:
                return "<value>"
        return template_value(candidate, root, depth=depth + 1, refs=refs)
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            for branch in branches:
                candidate = merge_schema(schema, branch, discard={key})
                if candidate is not None:
                    return template_value(candidate, root, depth=depth + 1, refs=refs)
            return "<value>"
    if "if" in schema:
        base = merge_schema(schema, True, discard={"if", "then", "else"})
        condition = schema["if"]
        preferred = schema.get("then", True)
        alternate = schema.get("else", True)
        if condition is False:
            candidate = merge_schema(base, alternate)
        else:
            candidate = merge_schema(base, condition)
            candidate = merge_schema(candidate, preferred) if candidate is not None else None
            if candidate is None:
                candidate = merge_schema(base, alternate)
        if candidate is not None:
            return template_value(candidate, root, depth=depth + 1, refs=refs)
        return "<value>"
    dependent_schemas = schema.get("dependentSchemas")
    if isinstance(dependent_schemas, dict) and dependent_schemas:
        base = merge_schema(schema, True, discard={"dependentSchemas"})
        for trigger, branch in dependent_schemas.items():
            candidate = merge_schema(base, {"required": [trigger]})
            candidate = merge_schema(candidate, branch)
            if candidate is not None:
                return template_value(candidate, root, depth=depth + 1, refs=refs)
    dependent_required = schema.get("dependentRequired")
    if isinstance(dependent_required, dict) and dependent_required:
        base = merge_schema(schema, True, discard={"dependentRequired"})
        trigger, required = next(iter(dependent_required.items()))
        candidate = merge_schema(base, {"required": [trigger, *required]})
        return template_value(candidate, root, depth=depth + 1, refs=refs)
    kind = schema.get("type")
    if kind == "object" or isinstance(schema.get("properties"), dict):
        properties = schema.get("properties", {})
        return {name: template_value(properties.get(name, {}), root, depth=depth + 1, refs=refs)
                for name in schema.get("required", []) if name in properties}
    if kind == "array":
        return [template_value(schema.get("items", {}), root, depth=depth + 1, refs=refs)]
    return "<" + str(kind or "value") + ">"


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
    if examples:
        result["examples"] = examples
    if schema_needs_template(schema):
        result["invocation"] = {
            "template": {"name": exposed["name"], "arguments": template_value(schema, schema)},
        }
    return result


def compact_tool(tool):
    result = {
        "gateway_name": tool["gateway_name"],
        "inputSchema": copy.deepcopy(tool.get("inputSchema", {})),
    }
    for key in ("title", "description", "outputSchema", "annotations", "examples", "invocation"):
        value = tool.get(key)
        if value not in (None, "", [], {}):
            result[key] = copy.deepcopy(value)
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
            requested_tools = arguments.get("tools")
            missing_tools = []
            if requested_tools is not None:
                requested = [(value, normalize(value)) for value in requested_tools]
                found = set()
                items = []
                for tool in selected_tools:
                    names = {normalize(tool["gateway_name"]), normalize(tool["original_name"])}
                    matches = {normalized for _value, normalized in requested if normalized in names}
                    if matches:
                        items.append(tool)
                        found.update(matches)
                missing_tools = [value for value, normalized in requested if normalized not in found]
                search_status["tools"] = {"mode": "exact", "semantic_status": "not_needed"}
            else:
                docs = [document(t["gateway_name"], [t["gateway_name"], t["original_name"]], [
                    (t["gateway_name"] + " " + t["original_name"] + " " + (t.get("title") or ""), 5),
                    (t.get("description") or "", 2), (schema_text(t["inputSchema"]), 3),
                    (t["mcp_name"] + " " + t["mcp_slug"], .5)]) for t in selected_tools]
                matched_tools, search_status["tools"] = await rank(
                    docs, query_text(arguments.get("tool", "")), state.embeddings, config,
                    embedding_scope + ":tools", authorize=authorize_embeddings)
                by_name = {t["gateway_name"]: t for t in selected_tools}
                items = [by_name[doc["id"]] | {"match": match} for doc, match in matched_tools]
        # Recheck permission/cache after slow external embedding requests before disclosing results.
        _, _, current_scope = await self.snapshot(request)
        if current_scope != scope:
            raise DiscoveryError("catalog_changed", "Permissions or catalog changed; retry the search.")
        query = {k: normalize(query_text(arguments.get(k, "")))
                 for k in ("query",) if name == "gateway_search_mcps"}
        public_items = items
        if name != "gateway_search_mcps":
            query = {
                "mcp": normalize(query_text(arguments.get("mcp", ""))),
                "tool": normalize(query_text(arguments.get("tool", ""))),
                "tools": [normalize(value) for value in arguments.get("tools", [])],
            }
            public_items = [compact_tool(item) for item in items]
        version = fingerprint([scope, name, query, fingerprint(config), public_items, search_status])
        page = paginate(public_items, cursor=arguments.get("cursor"), limit=arguments.get(
            "limit", 50 if name == "gateway_search_mcps" else 20), version=version, key=state.config.jwt_secret)
        if name == "gateway_search_mcps":
            page["search"] = search_status
            page["next_step"] = "Call gateway_search_tools with a returned mcp id."
            return result_json(page)
        cursor = page["next_cursor"]
        response = {
            "instructions": "Use gateway_call with gateway_name and arguments matching inputSchema.",
            "mcps": [{"id": item["id"], "name": item["name"], "slug": item["slug"]} for item in selected],
            "tools": page["items"],
            "truncated": bool(cursor),
            "returned": page["returned"],
            "total": page["total"],
        }
        if cursor:
            response["next_cursor"] = cursor
            response["hint"] = "Use a narrower tool/tools query or continue with next_cursor."
        if missing_tools:
            response["missing_tools"] = missing_tools
        return result_json(response)
