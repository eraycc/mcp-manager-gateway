"""Web catalog administration; every operation uses the shared runtime."""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .catalog import web_authorizer
from .database import McpServer, SystemSetting, get_setting
from .identity import admin_user, current_user, effective_mcp_ids
from .imports import deduplicate, normalize_import, scan_sources
from .runtime import GatewayError, ServerSpec
from .transports import validate_config

router = APIRouter(prefix="/api/v1")
ADMIN = Depends(admin_user)
USER = Depends(current_user)


async def permitted(request, server_id, user):
    row = await request.app.state.catalog.get(server_id)
    if user.role != "admin" and server_id not in await effective_mcp_ids(request.app.state.db, user):
        raise HTTPException(404, "MCP not found")
    return row


@router.get("/mcps")
async def listing(request: Request, q: str = "", page: int = 1, page_size: int = 20,
                  mode: str = "", transport: str = "", status: str = "", user=USER):
    if page < 1 or not 1 <= page_size <= 200:
        raise HTTPException(422, "Invalid pagination")
    if status not in {"", "running", "ready", "stopped", "failed"}:
        raise HTTPException(422, "Invalid status")
    cat = request.app.state.catalog
    allowed = None if user.role == "admin" else await effective_mcp_ids(cat.db, user)
    rows = [r for r in await cat.rows() if (allowed is None or r.id in allowed)
            and (not mode or r.mode == mode) and (not transport or r.transport == transport)
            and (not q or q.lower() in (r.name + " " + r.description + " " + " ".join(r.tags)).lower())]
    items = [
        cat.public(row, user_id=user.id, runtime_user_id=None if user.role == "admin" else user.id)
        for row in rows
    ]
    if status:
        items = [item for item in items if item["status"] == status]
    return {"items": items[(page - 1) * page_size:page * page_size],
            "total": len(items), "page": page, "page_size": page_size,
            "total_pages": (len(items) + page_size - 1) // page_size}


@router.post("/mcps")
async def create(data: dict, request: Request, user=ADMIN):
    cat = request.app.state.catalog
    row = await cat.create(data, authorize=web_authorizer(request), user_id=user.id)
    await request.app.state.logs.audit("mcp.create", user.id, {"mcp_id": row.id})
    return cat.public(row, detail=True, user_id=user.id)


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
    existing = [{"name": row.name, "transport": row.transport, "isolation": row.isolation,
                 "config": cat.unseal(row.config)} for row in await cat.rows()]
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
        result["refresh_job"] = request.app.state.jobs.submit(
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
        public = cat.public(row, detail=True, user_id=user.id)
        result[row.slug] = {k: public[k] for k in ("name", "description", "tags", "transport", "mode", "isolation", "config")}
        if data.get("include_secrets", False):
            result[row.slug]["config"] = cat.unseal(row.config)
    await request.app.state.logs.audit("mcp.export", user.id,
                                       {"count": len(result), "include_secrets": data.get("include_secrets", False)})
    return {"mcpServers": result}


@router.post("/mcps/diagnose")
async def diagnose(data: dict, request: Request, user=ADMIN):
    user = await web_authorizer(request)()
    transport = data.get("transport", "stdio")
    transport = "streamable-http" if transport == "http" else transport
    validate_config(transport, data.get("config", {}))
    runtime = request.app.state.runtime
    lease = runtime.create_lease(user.id, "diagnostic", kind="maintenance")
    try:
        return await runtime.discover(ServerSpec("diagnose-" + uuid4().hex, transport, data["config"]), lease.id)
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
    return request.app.state.jobs.submit("mcp." + action, ids, operation, user.id)


@router.get("/mcps/{server_id}")
async def detail(server_id: str, request: Request, user=USER):
    row = await permitted(request, server_id, user)
    return request.app.state.catalog.public(row, detail=user.role == "admin",
                                            user_id=user.id, runtime_user_id=None if user.role == "admin" else user.id)


@router.patch("/mcps/{server_id}")
async def update(server_id: str, data: dict, request: Request, user=ADMIN):
    row = await request.app.state.catalog.update(server_id, data, authorize=web_authorizer(request), user_id=user.id)
    await request.app.state.logs.audit("mcp.update", user.id, {"mcp_id": row.id, "revision": row.revision})
    return request.app.state.catalog.public(row, detail=True, user_id=user.id)


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
    values.update(slug=row.slug[:56] + "_" + uuid4().hex[:6], name=row.name[:120] + " Copy",
                  config=cat.unseal(row.config), mode="lazy")
    return cat.public(await cat.create(values, authorize=web_authorizer(request), user_id=user.id), detail=True, user_id=user.id)


@router.get("/mcps/{server_id}/tools")
async def cached_tools(server_id: str, request: Request, user=USER):
    row = await permitted(request, server_id, user)
    return request.app.state.catalog.cached(row, user.id)


@router.post("/mcps/{server_id}/refresh")
async def refresh(server_id: str, request: Request, user=ADMIN):
    cat = request.app.state.catalog
    if await cat.retry_disabled(server_id, user.id, authorize=web_authorizer(request)):
        return cat.cached(await cat.get(server_id), user.id)
    return await cat.refresh(server_id, user.id, authorize=web_authorizer(request))


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
    return {"cases": cases}


@router.post("/mcps/{server_id}/test-all")
async def test_all(server_id: str, request: Request, user=ADMIN):
    user = await web_authorizer(request)()
    await request.app.state.catalog.get(server_id)
    cases = await get_setting(request.app.state.db, "tests:" + server_id, [])
    cases = [c for c in cases if c.get("enabled", True)]
    async def operation(case):
        return await test_tool(server_id, case, request, user)
    return request.app.state.jobs.submit("mcp.test", cases, operation, user.id)
