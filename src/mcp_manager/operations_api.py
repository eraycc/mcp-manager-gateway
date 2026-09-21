"""Dashboard, JSONL log operations, settings and background jobs."""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from .catalog import web_authorizer
from .database import ApiToken, SystemSetting
from .identity import admin_user, current_user, effective_mcp_ids

router = APIRouter(prefix="/api/v1")
USER = Depends(current_user)
ADMIN = Depends(admin_user)
DEFAULTS = {"title": "MCP Manager", "registration_enabled": True, "jwt_days": 30,
            "token_auth_enabled": True, "anonymous_scope_mode": "selected", "anonymous_mcp_ids": [],
            "idle_seconds": 86400, "refresh_enabled": False, "refresh_cron": "0 3 * * *",
            "startup_failure_threshold": 3, "timezone": "Asia/Shanghai",
            "log_retention_days": 0, "cors_origins": ["*"], "allowed_hosts": ["*"]}
FILTERS = {"q", "username", "user_id", "token_id", "mcp_id", "tool_name", "status", "source",
           "from_time", "to_time"}


def log_filters(request, user, values=None):
    result = {k: v for k, v in (values or dict(request.query_params)).items() if k in FILTERS and v != ""}
    if user.role != "admin":
        result["user_id"] = user.id
    return result


@router.get("/dashboard")
async def dashboard(request: Request, user=USER):
    state = request.app.state
    scope = {} if user.role == "admin" else {"user_id": user.id}
    stats = await state.logs.stats(**scope)
    allowed = None if user.role == "admin" else await effective_mcp_ids(state.db, user)
    rows = [r for r in await state.catalog.rows() if allowed is None or r.id in allowed]
    visible_ids = {r.id for r in rows}
    runtime = [r for r in state.runtime.status(**scope) if r["server_id"] in visible_ids]
    running = {r["server_id"] for r in runtime if r["phase"] == "ready"}
    async with state.db.session() as s:
        query = select(func.count()).select_from(ApiToken)
        if user.role != "admin":
            query = query.where(ApiToken.user_id == user.id)
        token_count = await s.scalar(query)
    return stats | {"token_count": token_count, "mcp_count": len(rows), "running_count": len(running),
                    "stopped_count": len(rows) - len(running),
                    "lease_count": sum(1 for l in state.runtime.leases.values()
                                       if user.role == "admin" or l.user_id == user.id),
                    "connection_count": len([r for r in runtime if r["phase"] == "ready"]),
                    "recent": (await state.logs.query(page_size=10, **scope))["items"]}


@router.get("/tokens/{token_id}/stats")
async def token_stats(token_id: str, request: Request, user=USER):
    async with request.app.state.db.session() as s:
        token = await s.get(ApiToken, token_id)
        if not token or (user.role != "admin" and token.user_id != user.id):
            raise HTTPException(404, "Token not found")
    return await request.app.state.logs.stats(token_id=token_id)


@router.get("/logs")
async def logs(request: Request, page: int = 1, page_size: int = 20, user=USER):
    if page < 1 or not 1 <= page_size <= 200:
        raise HTTPException(422, "Invalid pagination")
    return await request.app.state.logs.query(page=page, page_size=page_size, **log_filters(request, user))


@router.get("/logs/export")
async def export_logs(request: Request, user=USER):
    filters = log_filters(request, user)
    store = request.app.state.logs
    # Freeze the upper time boundary; keyset pagination remains stable as older
    # records are deleted or newer calls arrive during export.
    from .database import now
    filters.setdefault("to_time", now().isoformat())
    async def stream():
        async with store.export_capacity:
            yield "["
            first, cursor = True, None
            while not await request.is_disconnected():
                await current_user(request)
                items, cursor = await store.export_batch(filters, cursor=cursor)
                for detail in items:
                    if await request.is_disconnected():
                        return
                    if not first:
                        yield ","
                    first = False
                    yield json.dumps(detail, ensure_ascii=False)
                if not items:
                    break
            yield "]"
    return StreamingResponse(stream(), media_type="application/json",
                             headers={"Content-Disposition": 'attachment; filename="mcp-logs.json"'})


@router.post("/logs/delete")
async def delete_logs(data: dict, request: Request, user=ADMIN):
    user = await web_authorizer(request)()
    filters = log_filters(request, user, data.get("filters", {}))
    ids = data.get("ids")
    if ids is not None and not isinstance(ids, list):
        raise HTTPException(422, "ids must be an array or null")
    async def operation(_):
        await web_authorizer(request)()
        result = await request.app.state.logs.delete(ids, **filters)
        await request.app.state.logs.audit("logs.delete", user.id, {"count": result})
        return result
    return request.app.state.jobs.submit("logs.delete", ["logs"], operation, user.id)


@router.post("/logs/rebuild")
async def rebuild_logs(request: Request, user=ADMIN):
    user = await web_authorizer(request)()
    async def operation(_):
        await web_authorizer(request)()
        return await request.app.state.logs.rebuild()
    return request.app.state.jobs.submit("logs.rebuild", ["index"], operation, user.id)


@router.get("/logs/{log_id}")
async def log_detail(log_id: str, request: Request, user=USER):
    result = await request.app.state.logs.detail(log_id, user_id=None if user.role == "admin" else user.id)
    if not result:
        raise HTTPException(404, "Log not found")
    return result


@router.get("/about")
async def about(user=USER):
    from .about import PROJECT
    return PROJECT


@router.get("/settings")
async def settings(request: Request, user=ADMIN):
    async with request.app.state.db.session() as s:
        rows = (await s.scalars(select(SystemSetting).where(SystemSetting.key.in_(list(DEFAULTS))))).all()
    return DEFAULTS | {r.key: r.value for r in rows}


@router.patch("/settings")
async def update_settings(data: dict, request: Request, user=ADMIN):
    if set(data) - set(DEFAULTS):
        raise HTTPException(422, "Unknown system setting")
    result = await settings(request, user) | data
    for key in ("registration_enabled", "token_auth_enabled", "refresh_enabled"):
        if not isinstance(result[key], bool):
            raise HTTPException(422, key + " must be a boolean")
    for key in ("jwt_days", "idle_seconds", "log_retention_days"):
        if not isinstance(result[key], (int, float)) or isinstance(result[key], bool) or result[key] < 0:
            raise HTTPException(422, key + " must be nonnegative")
    threshold = result["startup_failure_threshold"]
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 1 <= threshold <= 100:
        raise HTTPException(422, "startup_failure_threshold must be an integer from 1 to 100")
    if not isinstance(result["title"], str) or not 1 <= len(result["title"]) <= 128:
        raise HTTPException(422, "Title must contain 1–128 characters")
    if result["anonymous_scope_mode"] not in ("selected", "all") or not isinstance(result["anonymous_mcp_ids"], list):
        raise HTTPException(422, "Invalid anonymous scope")
    from .cors import validate_hosts, validate_origins
    validate_origins(result["cors_origins"])
    result["allowed_hosts"] = validate_hosts(result["allowed_hosts"])
    if "allowed_hosts" in data:
        data["allowed_hosts"] = result["allowed_hosts"]
    ZoneInfo(result["timezone"])
    CronTrigger.from_crontab(result["refresh_cron"], timezone=result["timezone"])
    async with request.app.state.db.locked() as s:
        user = await web_authorizer(request)(s)
        for key, value in data.items():
            row = await s.get(SystemSetting, key)
            if row:
                row.value = value
            else:
                s.add(SystemSetting(key=key, value=value))
    result = await settings(request, user)
    request.app.state.cors_origins = result["cors_origins"]
    request.app.state.allowed_hosts = result["allowed_hosts"]
    request.app.state.runtime.idle_seconds = result["idle_seconds"]
    request.app.state.catalog.startup_failure_threshold = result["startup_failure_threshold"]
    request.app.state.logs.timezone = ZoneInfo(result["timezone"])
    request.app.state.refresh_next = None
    if "log_retention_days" in data:
        request.app.state.retention_next = 0
    await request.app.state.logs.audit("settings.update", user.id, data)
    return result


@router.post("/settings/cron-preview")
async def cron_preview(data: dict, user=ADMIN):
    trigger = CronTrigger.from_crontab(data.get("expression", ""), timezone=data.get("timezone", "Asia/Shanghai"))
    current = datetime.now(ZoneInfo(data.get("timezone", "Asia/Shanghai")))
    runs, previous = [], None
    for _ in range(5):
        previous = trigger.get_next_fire_time(previous, current)
        if previous is None:
            break
        runs.append(previous.isoformat())
        current = previous
    return {"next_runs": runs}


@router.get("/jobs")
async def jobs(request: Request, user=USER):
    return [j for j in request.app.state.jobs.items.values() if user.role == "admin" or j["user_id"] == user.id]


@router.get("/jobs/{job_id}")
async def job(job_id: str, request: Request, user=USER):
    item = request.app.state.jobs.items.get(job_id)
    if not item or (user.role != "admin" and item["user_id"] != user.id):
        raise HTTPException(404, "Job not found")
    return item


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request, user=USER):
    async with request.app.state.db.locked() as s:
        user = await web_authorizer(request, require_admin=False)(s)
        await job(job_id, request, user)
        task = request.app.state.jobs.tasks.get(job_id)
        if task:
            task.cancel()
    return {"ok": True}


@router.get("/events")
async def events(request: Request, user=USER):
    async def stream():
        while not await request.is_disconnected():
            # Revalidate cookie revocation between updates.
            try:
                actor = await current_user(request)
            except HTTPException:
                break
            result = await dashboard(request, actor)
            yield "event: dashboard\ndata: " + json.dumps(result, default=str) + "\n\n"
            await asyncio.sleep(10)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
