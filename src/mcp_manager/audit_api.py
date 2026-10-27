"""Administrator-only audit inspection and recoverable background maintenance."""
import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from .audit_events import ACTIONS, AUDIT_CONTEXT, clean
from .audit_store import COLUMNS, FILTERS
from .catalog import web_authorizer
from .database import McpServer, get_setting
from .identity import admin_user

router = APIRouter(prefix="/api/v1/audit-logs", tags=["audit"])
ADMIN = Depends(admin_user)


def filters_for(request, values=None):
    raw = dict(request.query_params) if values is None else dict(values)
    if set(raw) - FILTERS - {"page", "page_size", "format"}:
        raise HTTPException(422, "Unknown audit filter")
    result = {key: value for key, value in raw.items() if key in FILTERS and value not in (None, "")}
    if any(not isinstance(value, str) or len(value) > 500 for value in result.values()):
        raise HTTPException(422, "Audit filters must be strings of at most 500 characters")
    for key, allowed in (("result", {"success", "failure", "pending", "unknown"}),
                         ("actor_type", {"user", "system", "anonymous"}),
                         ("source", {"web", "cli", "system", "gateway", "unknown"})):
        if key in result and result[key] not in allowed:
            raise HTTPException(422, "Invalid " + key)
    store = request.app.state.logs
    for key in ("from_time", "to_time"):
        if key in result:
            result[key] = store.normalize_time(result[key], default_timezone=store.timezone)
    if result.get("from_time") and result.get("to_time") and result["from_time"] > result["to_time"]:
        raise HTTPException(422, "Start time must not exceed end time")
    return result


def actor_for(request, user):
    return {"user_id": user.id, "username": user.username, "source": "web", "actor_type": "user",
            "ip": request.client.host if request.client else None,
            "request_id": (AUDIT_CONTEXT.get() or {}).get("request_id")}


async def resolve_mcp_names(request, events):
    """Fill missing names for old events without changing stored name snapshots."""
    ids = {event["target_id"] for event in events
           if event.get("target_type") == "mcp" and event.get("target_id") and not event.get("target_name")}
    if not ids:
        return
    async with request.app.state.db.session() as session:
        rows = await session.execute(select(McpServer.id, McpServer.name).where(McpServer.id.in_(ids)))
        names = {identity: clean(name) for identity, name in rows}
    for event in events:
        if event.get("target_type") == "mcp" and not event.get("target_name"):
            event["target_name"] = names.get(event.get("target_id"))


@router.get("")
async def query_audits(request: Request, page: int = Query(1, ge=1),
                       page_size: int = Query(20, ge=1, le=200), user=ADMIN):
    result = await request.app.state.logs.audits.query(page=page, page_size=page_size, **filters_for(request))
    await resolve_mcp_names(request, result["items"])
    await web_authorizer(request)()
    return result


@router.get("/status")
async def audit_status(request: Request, user=ADMIN):
    result = await request.app.state.logs.audits.status()
    result["retention_days"] = await get_setting(request.app.state.db, "audit_log_retention_days", 0)
    result["timezone"] = str(request.app.state.logs.timezone)
    await web_authorizer(request)()
    return result


@router.get("/actions")
async def audit_actions(request: Request, user=ADMIN):
    return {"items": ACTIONS}


def csv_line(values):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    # Spreadsheet formulas are executable even when correctly CSV quoted.
    writer.writerow(["'" + str(value) if str(value).lstrip().startswith(("=", "+", "-", "@", "\t", "\r"))
                     else value for value in values])
    return stream.getvalue()


@router.get("/export")
async def export_audits(request: Request, format: str = "json", user=ADMIN):
    if format not in {"json", "csv"}:
        raise HTTPException(422, "Export format must be json or csv")
    filters = filters_for(request)
    store = request.app.state.logs
    ceiling = await store.audits.watermark()
    await store.audit("audit.export", user.id, {"format": format, "filters": filters})
    async def stream():
        cursor, first = None, True
        if format == "json":
            yield "["
        else:
            yield "\ufeff" + csv_line([*COLUMNS, "details"])
        async with store.export_capacity:
            while not await request.is_disconnected():
                await web_authorizer(request)()
                rows, cursor = await store.audits.export_batch(max_seq=ceiling, cursor=cursor, **filters)
                if not rows:
                    break
                await resolve_mcp_names(request, rows)
                await web_authorizer(request)()
                for event in rows:
                    if format == "json":
                        yield ("" if first else ",") + json.dumps(event, ensure_ascii=False)
                    else:
                        yield csv_line([event.get(key) if event.get(key) is not None else "" for key in COLUMNS] +
                                       [json.dumps(event["details"], ensure_ascii=False)])
                    first = False
            if format == "json":
                yield "]"
    return StreamingResponse(stream(), media_type="application/json" if format == "json" else "text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit-logs.' + format + '"', "Cache-Control": "no-store"})


async def submit_delete(request, user, ids, filters, action):
    actor = actor_for(request, user)
    ceiling = await request.app.state.logs.audits.watermark()
    async def operation(_):
        await web_authorizer(request)()
        return await request.app.state.logs.audits.delete(ids, actor=actor, max_seq=ceiling, action=action, **filters)
    return await request.app.state.jobs.submit(action, ["audit"], operation, user.id)


@router.post("/batch-delete", include_in_schema=False)
@router.post("/delete")
async def delete_audits(data: dict, request: Request, user=ADMIN):
    if set(data) - {"ids", "filters", "confirm"}:
        raise HTTPException(422, "Unknown audit deletion option")
    ids = data.get("ids")
    if ids is not None and (not isinstance(ids, list) or not 1 <= len(ids) <= 500
                            or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in ids)):
        raise HTTPException(422, "Select between 1 and 500 event IDs")
    if ids is None and data.get("confirm") is not True:
        raise HTTPException(422, "Confirm deleting all matching audit events")
    if not isinstance(data.get("filters", {}), dict):
        raise HTTPException(422, "filters must be an object")
    filters = filters_for(request, data.get("filters", {}))
    await web_authorizer(request)()
    return await submit_delete(request, user, ids, filters, "audit.delete")


@router.delete("")
async def clear_audits(request: Request, confirm: bool = False, user=ADMIN):
    if not confirm:
        raise HTTPException(422, "Confirm clearing audit events")
    await web_authorizer(request)()
    return await submit_delete(request, user, None, {}, "audit.clear")


@router.post("/rebuild")
async def rebuild_audits(request: Request, user=ADMIN):
    async def operation(_):
        await web_authorizer(request)()
        await request.app.state.logs.audit("audit.rebuild", user.id, {"phase": "started"}, result="pending")
        await request.app.state.logs.audits.rebuild()
        await request.app.state.logs.audit("audit.rebuild", user.id, {"phase": "completed"})
        return {"rebuilt": True}
    return await request.app.state.jobs.submit("audit.rebuild", ["audit"], operation, user.id)


@router.get("/{event_id}")
async def audit_detail(event_id: str, request: Request, user=ADMIN):
    result = await request.app.state.logs.audits.detail(event_id)
    await web_authorizer(request)()
    if result is None:
        raise HTTPException(404, "Audit event not found")
    await resolve_mcp_names(request, [result])
    await web_authorizer(request)()
    return clean(result)


@router.delete("/{event_id}")
async def delete_audit(event_id: str, request: Request, user=ADMIN):
    if not await request.app.state.logs.audits.detail(event_id):
        raise HTTPException(404, "Audit event not found")
    await web_authorizer(request)()
    return await submit_delete(request, user, [event_id], {}, "audit.delete")
