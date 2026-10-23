"""Progressive MCP discovery: service summaries, full tool contracts, exact execution."""
import asyncio
import copy
import json

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from mcp import types
from referencing.exceptions import Unresolvable

from .paging import McpPageBuilder, decode_cursor
from .runtime import GatewayError
from .schema_validation import LOCAL_SCHEMA_REGISTRY
from .tool_index import document, fingerprint, normalize, query_text, rank, schema_text

PAGING = {
    "limit": {"type": "integer", "minimum": 1, "maximum": 100,
              "description": "1-100 (default 20)"},
    "cursor": {"type": "string", "maxLength": 2048,
               "description": "next_cursor from previous page; keep query/limit unchanged"},
}
DISCOVERY = [
    {"name": "gateway_search_mcps",
     "description": "Search authorized service summaries, not tool schemas; then gateway_search_tools. Paginated; does not start services.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "maxLength": 2000, "default": "",
                   "description": "Service name/slug/purpose; empty or '*'=browse"},
         **PAGING, "limit": {**PAGING["limit"], "maximum": 200, "default": 50,
                             "description": "1-200 (default 50)"}},
         "additionalProperties": False, "examples": [{"query": "files"}]},
     "annotations": {"readOnlyHint": True, "destructiveHint": False}},
    {"name": "gateway_search_tools",
     "description": "Search tools; return full inputSchema unless names_only. Use exact gateway_name with gateway_call. Paginated; does not start services.",
     "inputSchema": {"type": "object", "properties": {
         "mcp": {"type": "string", "maxLength": 2000, "default": "",
                 "description": "Service id/slug/name/purpose; prefer returned id; empty or '*'=all"},
         "tool": {"type": "string", "maxLength": 2000, "default": "",
                  "description": "Name/gateway_name/purpose/parameter keyword; empty or '*'=browse; cannot combine with tools"},
         "tools": {"type": "array",
                   "items": {"type": "string", "minLength": 1, "maxLength": 128},
                   "minItems": 1, "maxItems": 50, "uniqueItems": True,
                   "description": "Exact original names/gateway_names; cannot combine with tool"},
         "names_only": {"type": "boolean", "default": False,
                        "description": "Summaries only; fetch full inputSchema before gateway_call"},
         **PAGING}, "not": {"required": ["tool", "tools"]}, "additionalProperties": False,
         "examples": [{"mcp": "<service-id>", "tool": "<tool-name>"}]},
     "annotations": {"readOnlyHint": True, "destructiveHint": False}},
    {"name": "gateway_call",
     "description": "Call one discovered tool. Copy exact gateway_name and match arguments to its gateway_search_tools inputSchema; never guess names.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "minLength": 1, "maxLength": 128,
                  "description": "Exact gateway_name from gateway_search_tools"},
         "arguments": {"type": "object", "description": "Parameters matching the discovered tool's inputSchema"}},
         "required": ["name", "arguments"], "additionalProperties": False,
         "examples": [{"name": "<gateway_name>", "arguments": {}}]}},
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


def resource_search_document(item):
    """Only descriptive catalog fields may leave the gateway for semantic retrieval."""
    return {key: item[key] for key in ("name", "title", "description") if isinstance(item.get(key), str)}


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
        validator = Draft202012Validator(schema, registry=LOCAL_SCHEMA_REGISTRY)
        for example in schema.get("examples", []) if isinstance(schema, dict) else []:
            if isinstance(example, dict) and validator.is_valid(example):
                examples.append({"name": exposed["name"], "arguments": copy.deepcopy(example)})
            if len(examples) == 3:
                break
    except (SchemaError, TypeError, Unresolvable):
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


    async def summary_snapshot(self, request):
        user, token, allowed = await self.gateway.principal(request)
        catalog = self.gateway.app.state.catalog
        rows = [row for row in await catalog.rows(ids=allowed, metadata_only=True) if row.mode != "disabled"]
        expected = await self.gateway.search_scope(request, user, token, rows)
        scopes = await catalog.index_scopes(rows, user.id if user else None)
        indexed = await catalog.index.get_summaries(scopes.values(), preview=True)
        services = []
        for row in rows:
            cache = catalog.cached_view(row, user.id if user else None)
            if row.id in indexed:
                metadata = indexed[row.id]
                names = metadata["tools_preview"]
                description = row.description or metadata["generated_description"]
                description_source = "configured" if row.description.strip() else "generated" if description else "missing"
                count, status = metadata["tool_count"], metadata["cache_status"]
            else:
                preview = cache["tools"][:50]
                names = sorted(tool["name"] for tool in preview)
                description, description_source = service_description(row.description, preview)
                count, status = len(cache["tools"]), cache["cache_status"]
            failure = catalog.failure_details(row, user.id if user else None)
            diagnostic = indexed.get(row.id, {})
            reason = diagnostic.get("cache_error") or failure["failure_reason"]
            item = {
                "id": row.id, "mcp": row.id, "name": row.name, "slug": row.slug,
                "description": description, "description_source": description_source,
                "tags": row.tags, "tool_count": count, "tools_list": names,
                "catalog_status": status, "status": failure["status"],
                "startup_failure_count": failure["startup_failure_count"],
                "failure_reason": reason,
                "cache_error": diagnostic.get("cache_error") or cache["cache_error"],
                "cache_error_code": diagnostic.get("cache_error_code") or cache["cache_error_code"],
                "last_refresh_error": diagnostic.get("last_refresh_error") or cache["last_refresh_error"],
                "last_refresh_error_code": diagnostic.get("last_refresh_error_code") or cache["last_refresh_error_code"],
                "last_refresh_failure_at": diagnostic.get("last_refresh_failure_at") or cache["last_refresh_failure_at"],
                "recovery": failure["recovery"] if reason else None,
                "last_startup_error": cache["last_startup_error"],
                "last_startup_error_code": cache["last_startup_error_code"],
                "last_startup_failure_at": cache["last_startup_failure_at"],
                "failure_scope": failure["failure_scope"], "revision": row.revision,
            }
            if len(names) < count:
                item["tools_list_truncated"] = True
            services.append(item)
        services.sort(key=lambda value: (value["slug"], value["id"]))
        return user, token, rows, services, scopes, indexed, expected

    async def search(self, name, arguments, request):
        from .embedding_api import load_embedding_config

        arguments = validate_meta(name, arguments)
        user, token, rows, services, scopes, indexed, expected = await self.summary_snapshot(request)
        state, catalog = self.gateway.app.state, self.gateway.app.state.catalog
        config = await load_embedding_config(state)
        config_version = fingerprint(config)
        async def authorize():
            await self.gateway.revalidate_search_scope(request, expected)
            if fingerprint(await load_embedding_config(state)) != config_version:
                raise DiscoveryError("catalog_changed", "Retrieval settings changed during search")
        embedding_scope = fingerprint([user.id if user else "anonymous", token.id if token else ""])
        docs = [document(item["id"], [item["id"], item["slug"], item["name"]], [
            (item["name"] + " " + item["slug"], 5), (" ".join(item["tags"]), 3),
            (" ".join(item["tools_list"]), 2), (item["description"], 1),
        ]) for item in services]
        mcp_query = query_text(arguments.get("query" if name == "gateway_search_mcps" else "mcp", ""))
        matched, service_search = await rank(
            docs, mcp_query, state.embeddings, config, embedding_scope + ":mcps", authorize=authorize
        )
        by_id = {item["id"]: item for item in services}
        selected = [by_id[doc["id"]] | {"match": match} for doc, match in matched]
        await authorize()
        query = {key: arguments.get(key) for key in ("query", "mcp", "tool", "tools", "limit", "names_only") if key in arguments}
        version = fingerprint([expected, name, query, config_version])
        if name == "gateway_search_mcps":
            offset = decode_cursor(arguments.get("cursor"), version, state.config.jwt_secret)
            def envelope(page):
                return result_json(page | {
                    "search": {"services": service_search},
                    "next_step": "Call gateway_search_tools with a returned mcp id.",
                })
            builder = await asyncio.to_thread(
                McpPageBuilder, total=len(selected), offset=offset, limit=arguments.get("limit", 50),
                version=version, signing_key=state.config.jwt_secret, envelope=envelope,
            )
            for item in selected[offset:offset + builder.limit]:
                if not await asyncio.to_thread(builder.add, item):
                    break
            await authorize()
            return builder.finish()
        row_by_id = {row.id: row for row in rows}
        selected_rows = [row_by_id[item["id"]] for item in selected]
        legacy = [row for row in selected_rows if row.id not in indexed
                  and catalog.cached_view(row, user.id if user else None)["tools"]]
        if not legacy:
            return await self.indexed_tool_search(
                selected_rows,
                scopes,
                indexed,
                arguments,
                version,
                config,
                authorize,
            )
        return await self.compatibility_tool_search(
            selected_rows, scopes, indexed, arguments, version, embedding_scope, config, authorize
        )

    def tool_envelope(self, page, *, missing=None, names_only=False, search=None):
        # Provider metadata is charged and disclosed only for contracts in this page.
        providers = {entry["provider"]["id"]: entry["provider"] for entry in page["items"]}
        response = {
            "instructions": ("Query the exact tool name without names_only to obtain its complete inputSchema before gateway_call."
                             if names_only else "Use gateway_call with gateway_name and arguments matching inputSchema."),
            "mcps": sorted(providers.values(), key=lambda value: (value["slug"], value["id"])),
            "tools": [entry["tool"] for entry in page["items"]],
            "truncated": bool(page["next_cursor"]), "returned": page["returned"], "total": page["total"],
        }
        if names_only:
            response["names_only"] = True
        if page["next_cursor"]:
            response.update(next_cursor=page["next_cursor"], hint="Use a narrower tool/tools query or continue with next_cursor.")
        if missing:
            response["missing_tools"] = missing
        if search:
            response["search"] = search
        return result_json(response)

    @staticmethod
    def name_summary(name, gateway_name, summary):
        return {"name": name, "gateway_name": gateway_name,
                **{key: summary[key] for key in ("title", "description") if key in summary}}

    def page_entry(self, row, tool):
        return {"tool": tool, "provider": {"id": row.id, "name": row.name, "slug": row.slug}}

    async def indexed_tool_search(
        self,
        selected,
        scopes,
        summaries,
        arguments,
        version,
        config,
        authorize,
    ):
        catalog, state = self.gateway.app.state.catalog, self.gateway.app.state
        generations = [summaries[row.id]["generation"] for row in selected if summaries[row.id].get("generation")]
        offset = decode_cursor(arguments.get("cursor"), version, state.config.jwt_secret)
        requested = arguments.get("tools")
        query = query_text(arguments.get("tool", ""))
        exact_names = requested
        if query:
            exact_total, _entries = await catalog.index.query_tool_entries(generations, exact_names=[query], limit=0)
            if exact_total:
                exact_names, query = [query], ""
        search_status = {"mode": "keyword", "semantic_status": "not_needed"}
        if query and exact_names is None:
            total, candidates = await catalog.index.query_tool_entries(
                generations,
                query=query,
                offset=0,
                limit=200,
            )
            semantic, search_status = await state.embeddings.score_candidates(
                scopes,
                candidates,
                query,
                config,
                deadline=2.0,
                authorize=authorize,
            )
            if semantic:
                lexical_order = {
                    state.embeddings.candidate_key(entry): position
                    for position, entry in enumerate(candidates, 1)
                }
                semantic_order = {
                    key: position
                    for position, key in enumerate(
                        sorted(semantic, key=lambda item: (-semantic[item], item)),
                        1,
                    )
                }
                candidates.sort(
                    key=lambda entry: (
                        -(
                            1 / (60 + lexical_order[state.embeddings.candidate_key(entry)])
                            + (
                                1 / (60 + semantic_order[state.embeddings.candidate_key(entry)])
                                if state.embeddings.candidate_key(entry) in semantic_order
                                else 0
                            )
                        ),
                        entry.server_id,
                        entry.original_name,
                    )
                )
            entries = candidates[offset:offset + arguments.get("limit", 20)]
        else:
            total, entries = await catalog.index.query_tool_entries(
                generations,
                query=query,
                exact_names=exact_names,
                offset=offset,
                limit=arguments.get("limit", 20),
            )
        if arguments.get("cursor") and offset >= total:
            raise DiscoveryError("invalid_cursor", "Discovery cursor is outside the result")
        missing = []
        if requested:
            for value in requested:
                found, _entries = await catalog.index.query_tool_entries(generations, exact_names=[value], limit=0)
                if not found:
                    missing.append(value)
        builder = await asyncio.to_thread(
            McpPageBuilder, total=total, offset=offset, limit=arguments.get("limit", 20),
            version=version, signing_key=state.config.jwt_secret,
            envelope=lambda page: self.tool_envelope(
                page,
                missing=missing,
                names_only=arguments.get("names_only", False),
                search=search_status,
            ),
        )
        by_id = {row.id: row for row in selected}
        for entry in entries:
            row = by_id[entry.server_id]
            if arguments.get("names_only"):
                contract = self.name_summary(entry.original_name, entry.gateway_name, entry.summary)
            else:
                original = await catalog.index.get_definition(scopes[row.id], kind="tools", name=entry.original_name)
                if original is None:
                    raise DiscoveryError("catalog_changed", "Catalog changed while reading a page")
                full = await asyncio.to_thread(tool_contract, row, original, dict(original, name=entry.gateway_name))
                contract = await asyncio.to_thread(compact_tool, full)
            if not await asyncio.to_thread(builder.add, self.page_entry(row, contract)):
                break
        await authorize()
        return builder.finish()

    async def compatibility_tool_search(self, selected, scopes, indexed, arguments, version,
                                        embedding_scope, config, authorize):
        """Bounded pre-cutover adapter; never build contracts except for the returned page."""
        from .catalog import alias

        catalog, state = self.gateway.app.state.catalog, self.gateway.app.state
        records = {}
        for row in selected:
            if row.id in indexed:
                generations = [indexed[row.id]["generation"]] if indexed[row.id].get("generation") else []
                total, entries = await catalog.index.query_tool_entries(generations, limit=1001)
                if total > 1000:
                    raise DiscoveryError("catalog_migration_required", "Mixed legacy catalogs need explicit upgrade before large searches")
                for entry in entries:
                    records[entry.gateway_name] = (row, entry)
            else:
                for tool in catalog.cached_view(row, scopes[row.id].owner if row.isolation == "user" else None)["tools"]:
                    records[alias(row.slug, tool["name"])] = (row, tool)
            if len(records) > 1000:
                raise DiscoveryError("catalog_migration_required", "Legacy catalogs need explicit upgrade before large searches")
        requested = arguments.get("tools")
        missing = []
        if requested:
            found, candidates = set(), []
            for gateway_name, (row, value) in records.items():
                original_name = value["name"] if isinstance(value, dict) else value.original_name
                aliases = {normalize(gateway_name), normalize(original_name)}
                matches = {normalize(name) for name in requested} & aliases
                if matches:
                    candidates.append({"gateway_name": gateway_name})
                    found.update(matches)
            missing = [name for name in requested if normalize(name) not in found]
        else:
            docs = []
            for gateway_name, (row, value) in records.items():
                if isinstance(value, dict):
                    tool = value
                    descriptive_schema = schema_text(tool.get("inputSchema", {}))
                    original_name = tool["name"]
                else:
                    tool = value.summary
                    descriptive_schema, original_name = value.search_text, value.original_name
                docs.append(document(gateway_name, [gateway_name, original_name], [
                    (gateway_name + " " + original_name + " " + (tool.get("title") or ""), 5),
                    (tool.get("description") or "", 2), (descriptive_schema, 3),
                    (row.name + " " + row.slug, .5),
                ]))
            matched, _status = await rank(
                docs, query_text(arguments.get("tool", "")), state.embeddings, config,
                embedding_scope + ":tools", authorize=authorize
            )
            candidates = [{"gateway_name": doc["id"]} for doc, _match in matched]
        if requested or not query_text(arguments.get("tool", "")):
            candidates.sort(key=lambda value: (records[value["gateway_name"]][0].slug, value["gateway_name"]))
        offset = decode_cursor(arguments.get("cursor"), version, state.config.jwt_secret)
        builder = await asyncio.to_thread(
            McpPageBuilder, total=len(candidates), offset=offset, limit=arguments.get("limit", 20),
            version=version, signing_key=state.config.jwt_secret,
            envelope=lambda page: self.tool_envelope(page, missing=missing, names_only=arguments.get("names_only", False)),
        )
        for item in candidates[offset:offset + builder.limit]:
            row, value = records[item["gateway_name"]]
            if arguments.get("names_only"):
                original_name = value["name"] if isinstance(value, dict) else value.original_name
                summary = value if isinstance(value, dict) else value.summary
                contract = self.name_summary(original_name, item["gateway_name"], summary)
            else:
                original = value if isinstance(value, dict) else await catalog.index.get_definition(
                    scopes[row.id], kind="tools", name=value.original_name
                )
                if original is None:
                    raise DiscoveryError("catalog_changed", "Catalog changed while reading a page")
                full = await asyncio.to_thread(tool_contract, row, original, dict(original, name=item["gateway_name"]))
                contract = await asyncio.to_thread(compact_tool, full)
            if not await asyncio.to_thread(builder.add, self.page_entry(row, contract)):
                break
        await authorize()
        return builder.finish()
