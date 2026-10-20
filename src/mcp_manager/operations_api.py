"""Dashboard, JSONL log operations, settings and background jobs."""
import asyncio
import json
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from .catalog import web_authorizer
from .database import ApiToken, SystemSetting, get_setting, invalidate_setting_cache
from .identity import admin_user, current_user, effective_mcp_ids
from .translation import DEFAULT_TRANSLATION_CONFIG, normalize_translation_config
from .updates import DEFAULT_AUTO_CHECK_INTERVAL_SECONDS, cached_update_status, ignore_update, update_status

router = APIRouter(prefix="/api/v1")
USER = Depends(current_user)
ADMIN = Depends(admin_user)
DEFAULTS = {"title": "MCP Manager", "registration_enabled": True, "jwt_days": 30,
            "token_auth_enabled": True, "anonymous_scope_mode": "selected", "anonymous_mcp_ids": [],
            "idle_seconds": 86400, "connection_ttl_seconds": 43200, "refresh_enabled": False, "refresh_cron": "0 3 * * *",
            "bridge_heartbeat_seconds": 90, "bridge_missed_heartbeats": 3,
            "startup_failure_threshold": 3, "timezone": "Asia/Shanghai",
            "log_retention_days": 0, "cors_origins": ["*"], "allowed_hosts": ["*"],
            "update_auto_check": True, "update_check_interval_seconds": DEFAULT_AUTO_CHECK_INTERVAL_SECONDS,
            "translation_config": DEFAULT_TRANSLATION_CONFIG}
FILTERS = {"q", "username", "user_id", "token_id", "mcp_id", "tool_name", "status", "source",
           "from_time", "to_time"}


def format_sse_heartbeat():
    return ": heartbeat\n\n"


class DashboardEventBuffer:
    """Small per-user replay window for native EventSource reconnections."""

    def __init__(self, maxlen=32):
        self.sequence = 0
        self.events = deque(maxlen=maxlen)

    def publish(self, payload):
        self.sequence += 1
        event = (
            f"id: {self.sequence}\n"
            "event: dashboard\n"
            f"data: {json.dumps(payload, default=str)}\n\n"
        )
        self.events.append((self.sequence, event))
        return event

    def replay(self, last_event_id):
        try:
            cursor = int(last_event_id)
        except (TypeError, ValueError):
            cursor = 0
        return [event for event_id, event in self.events if event_id > cursor]


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
    capacity = state.runtime.capacity_status()
    public_capacity = {
        key: capacity[key]
        for key in (
            "active_business_calls",
            "queued_business_calls",
            "instances",
            "instance_waiters",
            "queue_delay_ms_last",
            "queue_delay_ms_max",
        )
    }
    return stats | {"token_count": token_count, "mcp_count": len(rows), "running_count": len(running),
                    "stopped_count": len(rows) - len(running), "runtime_capacity": public_capacity,
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
    return await request.app.state.jobs.submit("logs.delete", ["logs"], operation, user.id)


@router.post("/logs/rebuild")
async def rebuild_logs(request: Request, user=ADMIN):
    user = await web_authorizer(request)()
    async def operation(_):
        await web_authorizer(request)()
        return await request.app.state.logs.rebuild()
    return await request.app.state.jobs.submit("logs.rebuild", ["index"], operation, user.id)


@router.get("/logs/{log_id}/preview")
async def log_preview(log_id: str, request: Request, user=USER):
    result = await request.app.state.logs.preview(
        log_id, user_id=None if user.role == "admin" else user.id, max_bytes=65536)
    if not result:
        raise HTTPException(404, "Log not found")
    fresh = await current_user(request)
    if fresh.role != "admin" and fresh.id != user.id:
        raise HTTPException(404, "Log not found")
    return result


@router.get("/logs/{log_id}/download")
async def log_download(log_id: str, request: Request, user=USER):
    store = request.app.state.logs
    reference = await store.reference(log_id, user_id=None if user.role == "admin" else user.id)
    if not reference:
        raise HTTPException(404, "Log not found")
    async def stream():
        async for chunk in store.download_chunks(reference):
            fresh = await current_user(request)
            if fresh.role != "admin" and reference["user_id"] != fresh.id:
                return
            yield chunk
    return StreamingResponse(stream(), media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="mcp-log.json"',
                 "Cache-Control": "private, no-store"})


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


@router.get("/translation/settings")
async def translation_settings(request: Request, user=USER):
    async with request.app.state.db.session() as s:
        row = await s.get(SystemSetting, "translation_config")
    try:
        return normalize_translation_config(row.value if row else {})
    except ValueError:
        return normalize_translation_config({})


async def _update_status_response(request, user, *, force=False):
    enabled = await get_setting(request.app.state.db, "update_auto_check", True)
    if force or enabled:
        result = await update_status(request.app.state.db, user.id, force=force)
    else:
        result = await cached_update_status(request.app.state.db, user.id)
    return result | {"auto_check_enabled": enabled}


@router.get("/update-status")
async def get_update_status(request: Request, user=USER):
    return await _update_status_response(request, user)


@router.post("/update-status/check")
async def check_update_status(request: Request, user=USER):
    return await _update_status_response(request, user, force=True)


@router.post("/update-status/ignore")
async def ignore_update_status(request: Request, user=USER):
    result = await ignore_update(request.app.state.db, user.id)
    enabled = await get_setting(request.app.state.db, "update_auto_check", True)
    result["auto_check_enabled"] = enabled
    await request.app.state.logs.audit("update.ignore", user.id, {
        "ignored_version": result["ignored_version"],
    })
    return result


def normalize_bridge_settings(values):
    # Compatibility with the former interval=0 switch. Do not normalize PATCH
    # input: new zero intervals must fail validation.
    if type(values.get("bridge_heartbeat_seconds")) is int and values["bridge_heartbeat_seconds"] == 0:
        return values | {"bridge_heartbeat_seconds": 90, "bridge_missed_heartbeats": 0}
    return values


@router.get("/settings")
async def settings(request: Request, user=ADMIN):
    async with request.app.state.db.session() as s:
        rows = (await s.scalars(select(SystemSetting).where(SystemSetting.key.in_(list(DEFAULTS))))).all()
    return normalize_bridge_settings(DEFAULTS | {r.key: r.value for r in rows})


@router.patch("/settings")
async def update_settings(data: dict, request: Request, user=ADMIN):
    if set(data) - set(DEFAULTS):
        raise HTTPException(422, "Unknown system setting")
    if "translation_config" in data:
        try:
            data["translation_config"] = normalize_translation_config(data["translation_config"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    result = await settings(request, user) | data
    try:
        result["translation_config"] = normalize_translation_config(result["translation_config"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    for key in ("registration_enabled", "token_auth_enabled", "refresh_enabled", "update_auto_check"):
        if not isinstance(result[key], bool):
            raise HTTPException(422, key + " must be a boolean")
    for key in ("jwt_days", "idle_seconds", "log_retention_days"):
        if not isinstance(result[key], (int, float)) or isinstance(result[key], bool) or result[key] < 0:
            raise HTTPException(422, key + " must be nonnegative")
    update_interval = result["update_check_interval_seconds"]
    if type(update_interval) is not int or not 1 <= update_interval <= 2**31 - 1:
        raise HTTPException(422, "update_check_interval_seconds must be an integer from 1 to 2147483647")
    connection_ttl = result["connection_ttl_seconds"]
    if type(connection_ttl) is not int or not 0 <= connection_ttl <= 2**31 - 1:
        raise HTTPException(422, "connection_ttl_seconds must be an integer from 0 to 2147483647")
    for key, minimum, maximum in (("bridge_heartbeat_seconds", 1, 86400), ("bridge_missed_heartbeats", 0, 100)):
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise HTTPException(422, key + " must be an integer from " + str(minimum) + " to " + str(maximum))
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
        interval = await s.get(SystemSetting, "bridge_heartbeat_seconds")
        # Normalize only a still-legacy pair, under the write lock. Unrelated
        # PATCHes must not overwrite concurrent changes to the heartbeat policy.
        if interval is not None and type(interval.value) is int and interval.value == 0:
            data = {"bridge_heartbeat_seconds": 90, "bridge_missed_heartbeats": 0} | data
        for key, value in data.items():
            row = await s.get(SystemSetting, key)
            if row:
                row.value = value
            else:
                s.add(SystemSetting(key=key, value=value))
    invalidate_setting_cache(request.app.state.db, data)
    result = await settings(request, user)
    request.app.state.cors_origins = result["cors_origins"]
    request.app.state.allowed_hosts = result["allowed_hosts"]
    request.app.state.runtime.idle_seconds = result["idle_seconds"]
    request.app.state.runtime.connection_ttl_seconds = result["connection_ttl_seconds"]
    request.app.state.runtime.bridge_heartbeat_seconds = result["bridge_heartbeat_seconds"]
    request.app.state.runtime.bridge_missed_heartbeats = result["bridge_missed_heartbeats"]
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
async def jobs(request: Request, page: int = 1, page_size: int = 50, user=USER):
    result = await request.app.state.jobs.store.list_jobs(
        user_id=None if user.role == "admin" else user.id, page=page, limit=page_size)
    fresh = await current_user(request)
    if fresh.id != user.id or fresh.role != user.role:
        raise HTTPException(403, "Job access changed")
    return result


@router.get("/jobs/{job_id}")
async def job(job_id: str, request: Request, user=USER):
    item = await request.app.state.jobs.store.get_status(job_id)
    fresh = await current_user(request)
    if not item or (fresh.role != "admin" and item["user_id"] != fresh.id):
        raise HTTPException(404, "Job not found")
    from starlette.responses import Response
    etag = '"job-' + item["id"] + '-' + str(item["version"]) + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-cache"})
    from fastapi.responses import JSONResponse
    return JSONResponse(item, headers={"ETag": etag, "Cache-Control": "private, no-cache"})


async def authorized_job(job_id, request):
    fresh = await current_user(request)
    status = await request.app.state.jobs.store.get_status(job_id)
    if not status or (fresh.role != "admin" and status["user_id"] != fresh.id):
        raise HTTPException(404, "Job not found")
    return status


@router.get("/jobs/{job_id}/results")
async def job_results(job_id: str, request: Request, cursor: str | None = None, limit: int = 50, user=USER):
    await authorized_job(job_id, request)
    result = await request.app.state.jobs.store.list_results(job_id, cursor=cursor, limit=limit)
    await authorized_job(job_id, request)
    return result


@router.get("/jobs/{job_id}/results/{ordinal}")
async def job_result(job_id: str, ordinal: int, request: Request, user=USER):
    await authorized_job(job_id, request)
    store = request.app.state.jobs.store
    ref = await store.get_result_ref(job_id, ordinal)
    if not ref:
        raise HTTPException(404, "Job result not found")
    if ref.size_bytes > 65536:
        raise HTTPException(413, "Result exceeds preview budget; use the result download endpoint")
    result = await store.get_result(job_id, ordinal)
    await authorized_job(job_id, request)
    return result


@router.get("/jobs/{job_id}/results/{ordinal}/download")
async def job_result_download(job_id: str, ordinal: int, request: Request, user=USER):
    await authorized_job(job_id, request)
    store = request.app.state.jobs.store
    ref = await store.get_result_ref(job_id, ordinal)
    if not ref:
        raise HTTPException(404, "Job result not found")
    async def stream():
        async with store.artifacts.open_download(ref) as chunks:
            async for chunk in chunks:
                await authorized_job(job_id, request)
                yield chunk
    return StreamingResponse(stream(), media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="job-result.json"',
                 "Cache-Control": "private, no-store"})


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request, user=USER):
    await authorized_job(job_id, request)
    async with request.app.state.db.locked() as session:
        await web_authorizer(request, require_admin=False)(session)
    # Do not hold the database mutex while workers persist cancellation outcomes.
    await request.app.state.jobs.cancel(job_id)
    return {"ok": True}


@router.get("/events")
async def events(request: Request, user=USER):
    state = request.app.state
    buffers = getattr(state, "dashboard_events", None)
    if buffers is None:
        buffers = state.dashboard_events = {}
    buffer = buffers.setdefault(user.id, DashboardEventBuffer())

    async def stream():
        for event in buffer.replay(request.headers.get("last-event-id")):
            yield event
        loop = asyncio.get_running_loop()
        dashboard_at = 0.0
        heartbeat_at = loop.time()
        while not await request.is_disconnected():
            current = loop.time()
            if current >= dashboard_at:
                try:
                    actor = await current_user(request)
                except HTTPException:
                    break
                yield buffer.publish(await dashboard(request, actor))
                dashboard_at = current + 10
            if current - heartbeat_at >= 15:
                yield format_sse_heartbeat()
                heartbeat_at = current
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
