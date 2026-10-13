"""Progressive resource metadata pages; never inspect an unrelated tool Schema."""
import asyncio

from jsonschema import Draft202012Validator, ValidationError

from .discovery import result_json, validation_error
from .paging import McpPageBuilder, decode_cursor
from .runtime import GatewayError
from .tool_index import fingerprint

KINDS = (("resources", "resource"), ("templates", "template"), ("prompts", "prompt"))


def public_summary(row, kind, value):
    item = dict(value, kind=kind, mcp={"id": row.id, "name": row.name, "slug": row.slug})
    field = "uri" if kind == "resource" else "uriTemplate"
    if item.get(field):
        item[field] = "mcp-manager://" + row.id + "/" + item[field]
    if kind == "resource":
        item["read_with"] = {"tool": "gateway_read_resource", "arguments": {"uri": item["uri"]}}
    return item


async def resource_page(gateway, request, arguments):
    from .embedding_api import load_embedding_config
    from .gateway import RESOURCE_TOOLS

    try:
        Draft202012Validator(RESOURCE_TOOLS[0]["inputSchema"]).validate(arguments)
    except ValidationError as exc:
        raise validation_error(exc, name="gateway_list_resources") from None
    user, token, allowed = await gateway.principal(request)
    catalog, state = gateway.app.state.catalog, gateway.app.state
    rows = [row for row in await catalog.rows(ids=allowed, metadata_only=True) if row.mode != "disabled"]
    expected = await gateway.search_scope(request, user, token, rows)
    scopes = await catalog.index_scopes(rows, user.id if user else None)
    summaries = await catalog.index.get_summaries(scopes.values())
    mcp = arguments.get("mcp", "").strip().casefold()
    selected = [row for row in rows if not mcp or mcp == "*" or
                mcp in (row.id + " " + row.slug + " " + row.name).casefold()]
    legacy = [row for row in selected if row.id not in summaries and
              any(catalog.cached_view(row, user.id if user else None)[kind] for kind, _label in KINDS)]
    if legacy:
        count = sum(len(catalog.cached_view(row, user.id if user else None)[kind])
                    for row in legacy for kind, _label in KINDS)
        if count > 1000 or any(row.id in summaries for row in selected):
            raise GatewayError("catalog_migration_required", "Upgrade legacy resource catalogs before large or mixed searches")
        return await gateway.legacy_resource_page(request, user, token, rows, arguments)
    config_version = fingerprint(await load_embedding_config(state))
    query = arguments.get("keyword", "").strip()
    version = fingerprint([expected, "resource-metadata", config_version,
                           {key: value for key, value in arguments.items() if key != "cursor"}])
    offset = decode_cursor(arguments.get("cursor"), version, state.config.jwt_secret)
    generations = [summaries[row.id]["generation"] for row in selected if summaries[row.id].get("generation")]
    counts = []
    for kind, label in KINDS:
        count, _entries = await catalog.index.query_tool_entries(generations, kind=kind, query=query, limit=0)
        counts.append((kind, label, count))
    total = sum(count for _kind, _label, count in counts)
    if arguments.get("cursor") and offset >= total:
        raise GatewayError("invalid_cursor", "Resource cursor is outside the result")
    def envelope(page):
        return result_json(page | {"count": page["returned"],
                                  "search": {"mode": "keyword", "semantic_status": "pending_index"}})
    builder = await asyncio.to_thread(McpPageBuilder, total=total, offset=offset,
        limit=arguments.get("limit", 20), version=version, signing_key=state.config.jwt_secret, envelope=envelope)
    skipped = offset
    by_id = {row.id: row for row in selected}
    stopped = False
    for kind, label, count in counts:
        if skipped >= count:
            skipped -= count
            continue
        _count, entries = await catalog.index.query_tool_entries(generations, kind=kind, query=query,
            offset=skipped, limit=builder.limit - len(builder.items))
        skipped = 0
        for entry in entries:
            item = public_summary(by_id[entry.server_id], label, entry.summary)
            if not await asyncio.to_thread(builder.add, item):
                stopped = True
                break
        if stopped or len(builder.items) == builder.limit:
            break
    await gateway.revalidate_search_scope(request, expected)
    if fingerprint(await load_embedding_config(state)) != config_version:
        raise GatewayError("catalog_changed", "Retrieval settings changed during search")
    return builder.finish()
