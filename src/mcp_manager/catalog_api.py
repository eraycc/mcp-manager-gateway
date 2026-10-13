"""Web catalog administration; every operation uses the shared runtime."""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .catalog import restore, web_authorizer
from .database import McpServer, SystemSetting, get_setting, invalidate_setting_cache
from .diagnostics import diagnostic_secrets, sanitize_diagnostic_error
from .identity import admin_user, current_user, effective_mcp_ids
from .imports import deduplicate, normalize_import, scan_sources
from .runtime import GatewayError, ServerSpec
from .transports import validate_service_config

router = APIRouter(prefix="/api/v1")
ADMIN = Depends(admin_user)
USER = Depends(current_user)


async def permitted(request, server_id, user):
    row = await request.app.state.catalog.get(server_id)
    if user.role != "admin" and server_id not in await effective_mcp_ids(request.app.state.db, user):
        raise HTTPException(404, "MCP not found")
    return row


def lightweight_status(cat, row, user_id):
    if row.mode == "disabled":
        return "stopped"
    instances = [value for value in cat.runtime.status(user_id=user_id if row.isolation == "user" else None)
                 if value["server_id"] == row.id]
    if any(value["in_flight"] for value in instances):
        return "running"
    if any(value["phase"] == "ready" for value in instances):
        return "ready"
    saved = cat._cache_failures.get(cat.cache_key(row, user_id)) or cat.cache_store.items.get(cat.cache_key(row, user_id))
    data = saved["data"] if saved and saved["revision"] == row.revision else {}
    return "failed" if data.get("runtime_status") == "failed" else "stopped"


def lightweight_public(cat, row, user_id):
    from .catalog import FAILURE_DEFAULTS

    key = cat.cache_key(row, user_id)
    saved = cat._cache_failures.get(key) or cat.cache_store.items.get(key)
    cache = saved["data"] if saved and saved["revision"] == row.revision else {}
    item = {field: getattr(row, field) for field in (
        "id", "slug", "name", "description", "tags", "transport", "mode", "isolation", "revision", "created_at"
    )}
    item.update(
        auth_type=row.auth_type or "unknown", config_isolation=row.config_isolation,
        metadata_indexing_needed=row.auth_type is None,
        status=lightweight_status(cat, row, user_id), tool_count=len(cache.get("tools", [])),
        cache_at=cache.get("cache_at"), cache_status=cache.get("cache_status", "empty"),
        cache_error=cache.get("cache_error"), cache_error_code=cache.get("cache_error_code"),
        cache_attempt_at=cache.get("cache_attempt_at"), auto_disabled=cache.get("auto_disabled", False),
    )
    item.update({field: cache.get(field, default) for field, default in FAILURE_DEFAULTS.items()
                 if field != "failed_gateway_names"})
    item["failure_scope"] = "user" if row.isolation == "user" else "global"
    return item


@router.get("/mcps")
async def listing(request: Request, q: str = "", page: int = 1, page_size: int = 20,
                  mode: str = "", transport: str = "", status: str = "",
                  enabled_only: bool = False, user=USER):
    from sqlalchemy import Text, cast, func, or_, select
    from sqlalchemy.orm import load_only

    if page < 1 or not 1 <= page_size <= 200:
        raise HTTPException(422, "Invalid pagination")
    if status not in {"", "running", "ready", "stopped", "failed"}:
        raise HTTPException(422, "Invalid status")
    cat = request.app.state.catalog
    actor = await web_authorizer(request, require_admin=False)()
    filters = []
    if actor.role != "admin" and actor.scope_mode != "all":
        filters.append(McpServer.id.in_(actor.mcp_ids))
    if mode:
        filters.append(McpServer.mode == mode)
    if enabled_only:
        filters.append(McpServer.mode != "disabled")
    if transport:
        filters.append(McpServer.transport == transport)
    if q:
        pattern = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        filters.append(or_(*[func.lower(field).like(pattern, escape="\\") for field in (
            McpServer.name, McpServer.description, cast(McpServer.tags, Text)
        )]))
    # Never fetch/decrypt full configurations merely to render a service option or list.
    fields = [getattr(McpServer, name) for name in (
        "id", "slug", "name", "description", "tags", "transport", "mode", "isolation", "revision",
        "created_at", "auth_type", "config_isolation"
    )]
    statement = select(McpServer).options(load_only(*fields)).where(*filters).order_by(
        McpServer.created_at.desc(), McpServer.id
    )
    async with cat.db.session() as session:
        if status:
            # Runtime status is not a persisted SQL column; filter only tiny metadata candidates.
            candidates = list((await session.scalars(statement)).all())
            ids = [row.id for row in candidates if lightweight_status(cat, row, actor.id) == status]
            filters.append(McpServer.id.in_(ids))
            statement = statement.where(McpServer.id.in_(ids))
        total = await session.scalar(select(func.count()).select_from(McpServer).where(*filters))
        rows = list((await session.scalars(statement.offset((page - 1) * page_size).limit(page_size))).all())
    scopes = await cat.index_scopes(rows, actor.id)
    summaries = await cat.index.get_summaries(scopes.values())
    items = []
    for row in rows:
        item = lightweight_public(cat, row, actor.id)
        if row.mode != "disabled" and row.id in summaries:
            item.update(summaries[row.id])
        items.append(item)
    from .http_budget import ensure_json_budget

    result = {"items": items, "total": total, "page": page, "page_size": page_size,
              "total_pages": (total + page_size - 1) // page_size}
    await ensure_json_budget(result)
    fresh = await web_authorizer(request, require_admin=False)()
    if ((actor.id, actor.role, actor.scope_mode, sorted(actor.mcp_ids))
            != (fresh.id, fresh.role, fresh.scope_mode, sorted(fresh.mcp_ids))):
        raise GatewayError("permission_revoked", "MCP permissions changed during service query")
    if await cat.index_scopes(rows, fresh.id) != scopes:
        raise GatewayError("credential_changed", "MCP credential scope changed during service query")
    return result


@router.post("/mcps")
async def create(data: dict, request: Request, user=ADMIN):
    cat = request.app.state.catalog
    row = await cat.create(data, authorize=web_authorizer(request), user_id=user.id)
    await request.app.state.logs.audit("mcp.create", user.id, {"mcp_id": row.id})
    return await cat.detail(row, user.id)


@router.post("/mcps/import-preview")
async def preview(data: dict, response: Response, user=ADMIN):
    response.headers["Cache-Control"] = "no-store"
    return normalize_import(data.get("data", data), data.get("channel", "generic"))


@router.get("/mcps/import-sources")
async def import_sources(request: Request, response: Response, channel: str = "generic", user=ADMIN):
    await web_authorizer(request)()
    response.headers["Cache-Control"] = "no-store"
    result = await scan_sources(channel)
    await web_authorizer(request)()
    return result


@router.post("/mcps/import-deduplicate")
async def import_deduplicate(data: dict, request: Request, response: Response, user=ADMIN):
    await web_authorizer(request)()
    response.headers["Cache-Control"] = "no-store"
    parsed = normalize_import(data.get("data", []), data.get("channel", "generic"))
    cat = request.app.state.catalog
    existing = []
    for row in await cat.rows():
        existing.append({
            "name": row.name,
            "transport": row.transport,
            "isolation": row.isolation,
            "config": await cat.credentials.materialize(
                row, user.id, require=False
            ),
        })
    kept, duplicates = deduplicate(parsed["items"], existing)
    return {"items": kept, "duplicates": duplicates, "errors": parsed["errors"]}


@router.post("/mcps/import")
async def import_mcps(data: dict, request: Request, user=ADMIN):
    await web_authorizer(request)()
    parsed = normalize_import(data.get("data", data), data.get("channel", "generic"))
    cat = request.app.state.catalog
    results = []
    refresh_ids = []
    conflict = data.get("conflict", "skip")
    if conflict not in {"skip", "replace", "copy"}:
        raise HTTPException(422, "Unsupported name conflict policy")
    for item in parsed["items"]:
        try:
            existing = next((r for r in await cat.rows() if r.slug == item.get("slug")), None)
            if existing and conflict == "skip":
                results.append({"name": item["name"], "status": "skipped"})
                continue
            if existing and conflict == "replace":
                row = await cat.update(existing.id, item, authorize=web_authorizer(request), user_id=user.id)
            else:
                if existing:
                    item["slug"] = item["slug"][:57].rstrip("_") + "_" + uuid4().hex[:6]
                row = await cat.create(item, authorize=web_authorizer(request), user_id=user.id)
            results.append({"name": row.name, "id": row.id, "status": "imported"})
            if row.mode != "disabled":
                refresh_ids.append(row.id)
        except HTTPException as exc:
            if exc.status_code in {401, 403}:
                raise
            parsed["errors"].append({"name": item.get("name", ""), "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - report each import item without aborting the batch
            parsed["errors"].append({"name": item.get("name", ""), "error": str(exc)})
    result = {"results": results, "errors": parsed["errors"]}
    if data.get("refresh") is True and refresh_ids:
        async def refresh_imported(server_id):
            actor = await web_authorizer(request)()
            return await cat.refresh(server_id, actor.id, authorize=web_authorizer(request))
        result["refresh_job"] = await request.app.state.jobs.submit(
            "mcp.import.refresh", refresh_ids, refresh_imported, user.id)
    return result


@router.post("/mcps/export")
async def export_mcps(data: dict, request: Request, user=ADMIN):
    user = await web_authorizer(request)()
    cat = request.app.state.catalog
    result = {}
    for row in await cat.rows():
        if data.get("ids") and row.id not in data["ids"]:
            continue
        public = await cat.detail(row, user.id)
        result[row.slug] = {
            key: public[key]
            for key in (
                "name", "description", "tags", "transport",
                "mode", "isolation", "config",
            )
        }
        if data.get("include_secrets", False):
            result[row.slug]["config"] = await cat.credentials.materialize(
                row, user.id, require=False
            )
    await request.app.state.logs.audit("mcp.export", user.id,
                                       {"count": len(result), "include_secrets": data.get("include_secrets", False)})
    return {"mcpServers": result}


def has_redacted(value):
    if value == "[REDACTED]":
        return True
    if isinstance(value, dict):
        return any(has_redacted(item) for item in value.values())
    if isinstance(value, list):
        return any(has_redacted(item) for item in value)
    return False


@router.post("/mcps/diagnose")
async def diagnose(data: dict, request: Request, user=ADMIN):
    user = await web_authorizer(request)()
    cat = request.app.state.catalog
    transport = data.get("transport", "stdio")
    transport = "streamable-http" if transport == "http" else transport
    isolation = data.get("isolation", "service")
    config = data.get("config", {})
    secrets = diagnostic_secrets(config)
    server_id = data.get("server_id")
    if server_id:
        row = await cat.get(server_id)
        if data.get("revision") != row.revision:
            raise HTTPException(
                409, "MCP configuration changed; reload before diagnosis"
            )
        saved = await cat.credentials.materialize(
            row, user.id, require=False
        )
        secrets.extend(diagnostic_secrets(saved))
        config = restore(config, saved)
        if has_redacted(config):
            raise GatewayError(
                "auth_required", "Saved credential is unavailable"
            )
    validate_service_config(transport, isolation, config)
    runtime = request.app.state.runtime
    lease = runtime.create_lease(user.id, "diagnostic", kind="maintenance")
    try:
        spec = ServerSpec(
            "diagnose-" + uuid4().hex,
            transport,
            config,
            "lazy",
            isolation,
        )
        try:
            value = await runtime.discover(spec, lease.id)
            from .http_budget import ensure_json_budget

            if data.get("summary"):
                value = {
                    "tool_count": len(value.get("tools", [])),
                    "resource_count": len(value.get("resources", [])),
                    "prompt_count": len(value.get("prompts", [])),
                    "template_count": len(value.get("templates", [])),
                    "capability_errors": value.get("capability_errors", []),
                }
                await ensure_json_budget(value, max_bytes=16384)
            else:
                await ensure_json_budget(value, max_bytes=1048576)
            await web_authorizer(request)()
            return value
        except Exception as exc:
            exc = sanitize_diagnostic_error(exc, secrets)
            raise exc from None
    finally:
        await runtime.release(lease.id)


@router.post("/mcps/batch")
async def batch(data: dict, request: Request, user=ADMIN):
    user = await web_authorizer(request)()
    cat = request.app.state.catalog
    action = data.get("action")
    if action not in {"delete", "enable", "lazy", "disable", "refresh", "start", "stop"}:
        raise HTTPException(422, "Unsupported batch action")
    ids = list(dict.fromkeys(data.get("ids", [])))
    if not 1 <= len(ids) <= 500:
        raise HTTPException(422, "Select between 1 and 500 MCPs")
    async def operation(server_id):
        actor = await web_authorizer(request)()
        if action == "delete":
            return await cat.delete(server_id, authorize=web_authorizer(request))
        if action == "refresh":
            return await refresh(server_id, request, actor)
        if action == "start":
            return await start(server_id, request, actor)
        if action == "stop":
            return await stop(server_id, request, actor)
        row = await cat.update(server_id, {"mode": {"enable": "eager", "disable": "disabled", "lazy": "lazy"}[action]},
                               authorize=web_authorizer(request), user_id=user.id)
        result = cat.public(row, user_id=user.id)
        if action in {"enable", "lazy"} and result["cache_status"] in {"error", "auth_required"}:
            raise GatewayError(result["cache_error_code"] or "discovery_failed",
                               result["cache_error"] or "MCP discovery failed")
        return result
    return await request.app.state.jobs.submit("mcp." + action, ids, operation, user.id)


@router.get("/mcps/{server_id}")
async def detail(server_id: str, request: Request, response: Response,
                 reveal: bool = False, user=USER):
    row = await permitted(request, server_id, user)
    cat = request.app.state.catalog
    if user.role == "admin":
        if reveal:
            await web_authorizer(request)()
            response.headers["Cache-Control"] = "no-store"
        return await cat.detail(row, user.id, reveal=reveal)
    return cat.public(row, user_id=user.id, runtime_user_id=user.id)


@router.patch("/mcps/{server_id}")
async def update(server_id: str, data: dict, request: Request, user=ADMIN):
    row = await request.app.state.catalog.update(server_id, data, authorize=web_authorizer(request), user_id=user.id)
    await request.app.state.logs.audit("mcp.update", user.id, {"mcp_id": row.id, "revision": row.revision})
    return await request.app.state.catalog.detail(row, user.id)


@router.delete("/mcps/{server_id}")
async def remove(server_id: str, request: Request, user=ADMIN):
    await request.app.state.catalog.delete(server_id, authorize=web_authorizer(request))
    await request.app.state.logs.audit("mcp.delete", user.id, {"mcp_id": server_id})
    return {"ok": True}


@router.post("/mcps/{server_id}/copy")
async def duplicate(server_id: str, request: Request, user=ADMIN):
    await web_authorizer(request)()
    cat = request.app.state.catalog
    row = await cat.get(server_id)
    values = cat.public(row)
    values.update(
        slug=row.slug[:56] + "_" + uuid4().hex[:6],
        name=row.name[:120] + " Copy",
        config=await cat.credentials.materialize(row, user.id, require=False),
        mode="lazy",
    )
    copied = await cat.create(
        values, authorize=web_authorizer(request), user_id=user.id
    )
    return await cat.detail(copied, user.id)


async def indexed_permission(request, server_id, *, configuration=False):
    from sqlalchemy.orm import load_only

    actor = await web_authorizer(request, require_admin=False)()
    if actor.role != "admin" and actor.scope_mode != "all" and server_id not in actor.mcp_ids:
        raise HTTPException(404, "MCP not found")
    cat = request.app.state.catalog
    if configuration:
        row = await cat.get(server_id)
    else:
        async with cat.db.session() as session:
            row = await session.get(McpServer, server_id, options=[load_only(
                McpServer.id, McpServer.slug, McpServer.revision, McpServer.isolation, McpServer.mode
            )])
        if not row:
            raise HTTPException(404, "MCP not found")
    return actor, row


@router.get("/mcps/{server_id}/capabilities")
async def capabilities(server_id: str, request: Request, kind: str = "tools", q: str = "",
                       cursor: str | None = None, limit: int = 50, user=USER):
    actor, row = await indexed_permission(request, server_id)
    cat = request.app.state.catalog
    scope = await cat.index_scope(row, actor.id)
    page = await cat.index.list_capabilities(scope, kind=kind, query=q, cursor=cursor, limit=limit)
    fresh_actor, fresh_row = await indexed_permission(request, server_id)
    if actor.id != fresh_actor.id or await cat.index_scope(fresh_row, fresh_actor.id) != scope:
        raise GatewayError("permission_revoked", "MCP scope changed during capability query")
    if ((await cat.index.get_summary(scope))["generation"] or "") != page.generation:
        raise GatewayError("revision_changed", "MCP catalog changed during capability query")
    return {"items": page.items, "total": page.total, "next_cursor": page.next_cursor,
            "generation": page.generation}


@router.get("/mcps/{server_id}/capabilities/{kind}/{name:path}")
async def capability_definition(server_id: str, kind: str, name: str, request: Request, user=USER):
    actor, row = await indexed_permission(request, server_id)
    cat = request.app.state.catalog
    scope = await cat.index_scope(row, actor.id)
    value = await cat.index.get_definition(scope, kind=kind, name=name)
    fresh_actor, fresh_row = await indexed_permission(request, server_id)
    if actor.id != fresh_actor.id or await cat.index_scope(fresh_row, fresh_actor.id) != scope:
        raise GatewayError("permission_revoked", "MCP scope changed during definition query")
    if value is None:
        raise HTTPException(404, "Capability not found")
    return value


@router.get("/mcps/{server_id}/tools")
async def cached_tools(server_id: str, request: Request, user=USER):
    actor, row = await indexed_permission(request, server_id, configuration=True)
    cat = request.app.state.catalog
    from .http_budget import ensure_json_budget

    # Reject the old full-list response before its historical double deepcopy.
    key = cat.cache_key(row, actor.id)
    record = cat._cache_failures.get(key) or cat.cache_store.items.get(key)
    if record and record["revision"] == row.revision:
        await ensure_json_budget(record["data"], max_bytes=1048576)
    value = cat.cached(row, actor.id)
    if row.auth_type in {None, "bearer", "oauth"}:
        try:
            await cat.credentials.materialize(row, actor.id)
        except GatewayError as exc:
            if exc.code != "auth_required":
                raise
            value = value | {key: [] for key in ("tools", "resources", "prompts", "templates")}
            value.update(cache_status="auth_required", cache_error=str(exc), cache_error_code=exc.code)
    await ensure_json_budget(value, max_bytes=1048576)
    await indexed_permission(request, server_id)
    return value


@router.post("/mcps/{server_id}/refresh")
async def refresh(server_id: str, request: Request, user=ADMIN):
    cat = request.app.state.catalog
    if await cat.retry_disabled(server_id, user.id, authorize=web_authorizer(request)):
        result = cat.cached(await cat.get(server_id), user.id)
    else:
        result = await cat.refresh_index(server_id, user.id, authorize=web_authorizer(request))
    from .http_budget import ensure_json_budget

    await ensure_json_budget(result, max_bytes=1048576)
    await web_authorizer(request)()
    return result


@router.post("/mcps/{server_id}/start")
async def start(server_id: str, request: Request, user=ADMIN):
    cat = request.app.state.catalog
    await cat.retry_disabled(server_id, user.id, authorize=web_authorizer(request), allow_manual=True)
    await cat.warm(server_id, user.id, explicit=True, authorize=web_authorizer(request))
    return {"ok": True}


@router.post("/mcps/{server_id}/stop")
async def stop(server_id: str, request: Request, user=ADMIN):
    await request.app.state.catalog.get(server_id)
    await web_authorizer(request)()
    await request.app.state.runtime.stop_server(server_id, hold=True)
    return {"ok": True}


@router.post("/mcps/{server_id}/test")
async def test_tool(server_id: str, data: dict, request: Request, user=ADMIN):
    user = await web_authorizer(request)()
    state = request.app.state
    row = await state.catalog.get(server_id)
    lease = state.runtime.create_lease(user.id, "test", kind="maintenance")
    try:
        return await state.catalog.call(row, lease, data["tool"], data.get("arguments", {}),
                                        user=user, authorize=web_authorizer(request), source="test")
    finally:
        await state.runtime.release(lease.id)


@router.get("/mcps/{server_id}/test-cases")
async def test_cases(server_id: str, request: Request, user=ADMIN):
    await request.app.state.catalog.get(server_id)
    return {"cases": await get_setting(request.app.state.db, "tests:" + server_id, [])}


@router.put("/mcps/{server_id}/test-cases")
async def save_cases(server_id: str, data: dict, request: Request, user=ADMIN):
    await request.app.state.catalog.get(server_id)
    cases = data.get("cases", [])
    if not isinstance(cases, list) or any(not c.get("tool") for c in cases):
        raise HTTPException(422, "Every test case needs a tool")
    async with request.app.state.db.locked() as s:
        await web_authorizer(request)(s)
        if not await s.get(McpServer, server_id):
            raise HTTPException(404, "MCP not found")
        key = "tests:" + server_id
        row = await s.get(SystemSetting, key)
        if row:
            row.value = cases
        else:
            s.add(SystemSetting(key=key, value=cases))
    invalidate_setting_cache(request.app.state.db, [key])
    return {"cases": cases}


@router.post("/mcps/{server_id}/test-all")
async def test_all(server_id: str, request: Request, user=ADMIN):
    user = await web_authorizer(request)()
    await request.app.state.catalog.get(server_id)
    cases = await get_setting(request.app.state.db, "tests:" + server_id, [])
    cases = [c for c in cases if c.get("enabled", True)]
    async def operation(case):
        return await test_tool(server_id, case, request, user)
    return await request.app.state.jobs.submit("mcp.test", cases, operation, user.id)
