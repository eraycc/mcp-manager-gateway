"""Progressive resource metadata, shared semantic retrieval and session snapshots."""
import asyncio
from difflib import SequenceMatcher

from jsonschema import Draft202012Validator, ValidationError

from .discovery import result_json, validation_error
from .paging import McpPageBuilder, bounded_mcp_page, decode_cursor
from .runtime import GatewayError
from .tool_index import fingerprint, tokens

KINDS = (("resources", "resource"), ("templates", "template"), ("prompts", "prompt"))
MAX_CANDIDATES = 200


def public_summary(row, kind, value):
    item = dict(value, kind=kind, mcp={"id": row.id, "name": row.name, "slug": row.slug})
    field = "uri" if kind == "resource" else "uriTemplate"
    if item.get(field) and not item.get("temporary"):
        item[field] = "mcp-manager://" + row.id + "/" + item[field]
    if kind == "resource":
        item["read_with"] = {"tool": "gateway_read_resource", "arguments": {"uri": item["uri"]}}
    elif kind == "prompt":
        from .prompt_resources import prompt_read_hint
        item.update(prompt_read_hint(row, item))
    return item


def matches(item, arguments):
    kind = arguments.get("kind", "all")
    if kind != "all" and kind != item["kind"]:
        return False
    prefix = arguments.get("uri_prefix", "")
    uri = item.get("source_uri", item.get("uri", item.get("uriTemplate", "")))
    if not item.get("temporary"):
        provider = item.get("mcp", {}).get("id")
        uri = uri.removeprefix("mcp-manager://" + str(provider) + "/")
    if prefix and (item["kind"] == "prompt" or not uri.startswith(prefix)):
        return False
    query = arguments.get("keyword", "").strip()
    if not query:
        return True
    if arguments.get("match") == "exact":
        return item.get("name") == query
    text = " ".join(str(item.get(key, "")) for key in ("name", "title", "description", "source_uri")).casefold()
    if query.casefold() in text:
        return True
    return arguments.get("match", "fuzzy") == "fuzzy" and typo_match(query, text)


def typo_match(query, text):
    return any(len(a) >= 4 and SequenceMatcher(None, a, b).ratio() >= .8
               for a in tokens(query)[:16] for b in tokens(text)[:512])


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
    authorize = lambda: gateway.revalidate_search_scope(request, expected)
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
    config = await load_embedding_config(state)
    config_version = fingerprint(config)
    temporary = await gateway.temporary_summaries(request, user, token, selected)
    by_id = {row.id: row for row in selected}
    dynamic = [public_summary(by_id[server], "resource", value) for server, value in temporary]
    dynamic = await asyncio.to_thread(lambda: [item for item in dynamic if matches(item, arguments)])
    version_parts = [expected, "resources-v2", config_version, temporary,
                     {key: value for key, value in arguments.items() if key != "cursor"}]
    generations = [summaries[row.id]["generation"] for row in selected
                   if summaries.get(row.id, {}).get("generation")]
    query = arguments.get("keyword", "").strip()
    match = arguments.get("match", "fuzzy")
    kinds = [(kind, label) for kind, label in KINDS if arguments.get("kind", "all") in ("all", label)]
    prefix = arguments.get("uri_prefix", "")
    if prefix:
        kinds = [(kind, label) for kind, label in kinds if kind != "prompts"]
    search = {"mode": "keyword", "semantic_status": "not_needed"}

    def envelope(page):
        return result_json(page | {"count": page["returned"], "search": search})

    async def check():
        await authorize()
        if fingerprint(await load_embedding_config(state)) != config_version:
            raise GatewayError("catalog_changed", "Retrieval settings changed during search")
        if await gateway.temporary_summaries(request, user, token, selected) != temporary:
            raise GatewayError("catalog_changed", "Temporary resource catalog changed during search")

    if query and match == "fuzzy":
        # Bound metadata/vector reads independently of catalog size. Lexical hits
        # take priority; a deterministic supplemental window also permits synonyms.
        lexical, supplemental, labels = [], [], {}
        for kind, label in kinds:
            _total, entries = await catalog.index.query_tool_entries(
                generations, kind=kind, query=query, limit=MAX_CANDIDATES, uri_prefix=prefix)
            lexical.extend(entries)
            labels.update({entry.id: label for entry in entries})
            _total, entries = await catalog.index.query_tool_entries(
                generations, kind=kind, limit=MAX_CANDIDATES, uri_prefix=prefix)
            supplemental.extend(entries)
            labels.update({entry.id: label for entry in entries})
        candidates = list({entry.id: entry for entry in [*lexical, *supplemental]}.values())[:MAX_CANDIDATES]
        lexical_ids = {entry.id for entry in lexical}
        lexical_ids.update(await asyncio.to_thread(
            lambda: {entry.id for entry in candidates if typo_match(query, entry.search_text)}))
        scores, search = await state.embeddings.score_candidates(
            scopes, candidates, query, config, deadline=2.0, authorize=authorize)
        semantic_order = {key: pos for pos, key in enumerate(sorted(scores, key=lambda k: (-scores[k], k)), 1)}
        lexical_order = {entry.id: pos for pos, entry in enumerate(
            [entry for entry in candidates if entry.id in lexical_ids], 1)}
        candidates = [entry for entry in candidates if entry.id in lexical_ids or
                      state.embeddings.candidate_key(entry) in scores]
        def rank(entry):
            semantic = semantic_order.get(state.embeddings.candidate_key(entry))
            lexical_rank = lexical_order.get(entry.id)
            score = (1 / (60 + lexical_rank) if lexical_rank else 0) + (1 / (60 + semantic) if semantic else 0)
            return -score, entry.server_id, entry.original_name
        candidates.sort(key=rank)
        items = [public_summary(by_id[entry.server_id], labels[entry.id], entry.summary) for entry in candidates]
        items.extend(dynamic)
        version = fingerprint([*version_parts, [item.get("uri", item.get("uriTemplate", item.get("name")))
                                               for item in items], search])
        page = await asyncio.to_thread(bounded_mcp_page, items, envelope=envelope,
            cursor=arguments.get("cursor"), limit=arguments.get("limit", 20), version=version,
            signing_key=state.config.jwt_secret)
        await check()
        return envelope(page)

    options = {"uri_prefix": prefix}
    if query and match == "exact":
        options["display_name"] = query
    elif query:
        options["query"] = query
        search["semantic_status"] = "skipped"
    counts = []
    for kind, label in kinds:
        count, _entries = await catalog.index.query_tool_entries(generations, kind=kind, limit=0, **options)
        counts.append((kind, label, count))
    total = sum(count for _kind, _label, count in counts) + len(dynamic)
    version = fingerprint(version_parts)
    offset = decode_cursor(arguments.get("cursor"), version, state.config.jwt_secret)
    if arguments.get("cursor") and offset >= total:
        raise GatewayError("invalid_cursor", "Resource cursor is outside the result")
    builder = await asyncio.to_thread(McpPageBuilder, total=total, offset=offset,
        limit=arguments.get("limit", 20), version=version, signing_key=state.config.jwt_secret, envelope=envelope)
    skipped = offset
    for kind, label, count in counts:
        if skipped >= count:
            skipped -= count
            continue
        _count, entries = await catalog.index.query_tool_entries(generations, kind=kind,
            offset=skipped, limit=builder.limit - len(builder.items), **options)
        skipped = 0
        for entry in entries:
            if not await asyncio.to_thread(builder.add, public_summary(by_id[entry.server_id], label, entry.summary)):
                await check()
                return builder.finish()
        if len(builder.items) == builder.limit:
            await check()
            return builder.finish()
    for item in dynamic[skipped:]:
        if not await asyncio.to_thread(builder.add, item):
            break
    await check()
    return builder.finish()
