"""Web-only observation and release of business leases; no downstream discovery."""
import math
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .database import ApiToken, McpServer, User
from .identity import current_user

router = APIRouter(prefix="/api/v1/leases", tags=["connections"])
USER = Depends(current_user)
BUSINESS_KINDS = {"agent", "bridge", "retention"}


def iso(timestamp):
    return None if timestamp is None else datetime.fromtimestamp(timestamp, UTC).isoformat()


def time_bound(value):
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result.timestamp()
    except (ValueError, OverflowError):
        raise HTTPException(422, "Use an ISO timestamp with a timezone") from None


async def snapshots(request, actor):
    state = request.app.state
    rows = state.runtime.lease_status(user_id=None if actor.role == "admin" else actor.id)
    rows = [r for r in rows if r["kind"] in BUSINESS_KINDS]
    user_ids = {r["user_id"] for r in rows}
    token_ids = {r["token_id"] for r in rows if r["user_id"] != "anonymous"}
    server_ids = {i["server_id"] for r in rows for i in r["instances"]}
    # Select display metadata only, never configs, password hashes or token secrets.
    async with state.db.session() as session:
        users = dict((await session.execute(
            select(User.id, User.username).where(User.id.in_(user_ids)))).all())
        tokens = {row.id: row for row in (await session.execute(
            select(ApiToken.id, ApiToken.user_id, ApiToken.name).where(ApiToken.id.in_(token_ids)))).all()}
        servers = {row.id: row for row in (await session.execute(
            select(McpServer.id, McpServer.name, McpServer.slug).where(McpServer.id.in_(server_ids)))).all()}
    fresh = await current_user(request)
    if fresh.id != actor.id or fresh.role != actor.role:
        raise HTTPException(403, "Connection visibility changed; refresh")
    for row in rows:
        anonymous = row["user_id"] == "anonymous"
        token = tokens.get(row["token_id"])
        row["username"] = "匿名" if anonymous else users.get(row["user_id"], "已删除用户")
        row["token_name"] = None if anonymous or not token or token.user_id != row["user_id"] else token.name
        if anonymous:
            row["token_id"] = None
        for key in ("created_at", "touched_at", "expires_at", "bridge_disconnect_at"):
            row[key] = iso(row[key])
        row["can_disconnect"] = True
        for instance in row["instances"]:
            server = servers.get(instance["server_id"])
            instance["name"] = server.name if server else instance["server_id"]
            instance["slug"] = server.slug if server else ""
    return rows


@router.get("")
async def list_leases(
    request: Request, user=USER,
    q: Annotated[str, Query(max_length=200)] = "", user_id: str = "", mcp_id: str = "",
    kind: Literal["", "agent", "bridge", "retention"] = "",
    status: Literal["", "active", "idle", "expiring", "expired"] = "",
    touched_from: str | None = None, touched_to: str | None = None,
    sort: Literal["touched_desc", "created_desc", "ttl_asc", "in_flight_desc"] = "touched_desc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
):
    start, end = time_bound(touched_from), time_bound(touched_to)
    if start is not None and end is not None and start > end:
        raise HTTPException(422, "Heartbeat range is reversed")
    rows = await snapshots(request, user)
    visible_ids = [r["id"] for r in rows]
    refs, users = {}, {}
    for row in rows:
        users[row["user_id"]] = {"user_id": row["user_id"], "username": row["username"]}
        for server_id in {i["server_id"] for i in row["instances"]}:
            instance = next(i for i in row["instances"] if i["server_id"] == server_id)
            ref = refs.setdefault(server_id, {"mcp_id": server_id, "mcp_name": instance["name"], "ref_count": 0})
            ref["ref_count"] += 1
    query = q.casefold().strip()
    def matches(row):
        touched = datetime.fromisoformat(row["touched_at"]).timestamp()
        text = " ".join(str(row.get(k) or "") for k in
                        ("id", "user_id", "username", "token_id", "token_name"))
        text += " " + " ".join(str(i.get(k) or "") for i in row["instances"]
                              for k in ("server_id", "name", "slug"))
        return (
            (not user_id or user.role != "admin" or row["user_id"] == user_id)
            and (not mcp_id or any(i["server_id"] == mcp_id for i in row["instances"]))
            and (not kind or row["kind"] == kind) and (not status or row["status"] == status)
            and (start is None or touched >= start) and (end is None or touched <= end)
            and (not query or query in text.casefold())
        )
    rows = [r for r in rows if matches(r)]
    def sort_key(row):
        if sort == "ttl_asc":
            return row["expires_in"] if row["expires_in"] is not None else math.inf
        if sort == "in_flight_desc":
            return -row["in_flight"]
        return -datetime.fromisoformat(row["created_at" if sort == "created_desc" else "touched_at"]).timestamp()
    rows.sort(key=lambda r: (sort_key(r), r["id"]))
    return {
        "items": rows[(page - 1) * page_size:page * page_size], "total": len(rows),
        "page": page, "page_size": page_size, "total_pages": max(1, math.ceil(len(rows) / page_size)),
        "mcp_refs": sorted(refs.values(), key=lambda x: (x["mcp_name"], x["mcp_id"])),
        "user_refs": sorted(users.values(), key=lambda x: (x["username"], x["user_id"])),
        "visible_ids": visible_ids,
    }


@router.get("/{lease_id}")
async def lease_detail(lease_id: str, request: Request, user=USER):
    rows = await snapshots(request, user)
    target = next((r for r in rows if r["id"] == lease_id), None)
    if target is None:
        raise HTTPException(404, "Lease not found")
    generations = {i["generation"] for i in target["instances"]}
    target["shared_leases"] = [{
        "lease_id": row["id"], "username": row["username"], "kind": row["kind"],
        "touched_at": row["touched_at"],
        "generations": sorted(generations & {i["generation"] for i in row["instances"]}),
    } for row in rows if row is not target
        and generations & {i["generation"] for i in row["instances"]}]
    return target


async def disconnect_one(lease_id, request):
    actor = await current_user(request)
    state = request.app.state
    target = state.runtime.leases.get(lease_id)
    if target and (target.kind not in BUSINESS_KINDS or
                   (actor.role != "admin" and target.user_id != actor.id)):
        raise HTTPException(403, "Cannot disconnect this lease")
    # Missing ids are idempotent, but stale session bindings still require ownership.
    for bound in state.gateway.sessions.values():
        if (lease_id in {bound.get("lease"), bound.get("explicit_lease")}
                and actor.role != "admin" and bound["owner"][0] != actor.id):
            raise HTTPException(403, "Cannot disconnect this lease")
    result = await state.gateway.disconnect_lease(lease_id)
    try:
        await state.logs.audit("lease.disconnect", actor.id, {"lease_id": lease_id, **result})
    except Exception:  # noqa: BLE001 - releasing succeeded; report audit failure without replaying it
        result = {**result, "ok": False, "audit_ok": False}
    return {"lease_id": lease_id, **result}


@router.post("/{lease_id}/disconnect")
async def disconnect(lease_id: str, request: Request, user=USER):
    return await disconnect_one(lease_id, request)


class LeaseBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(min_length=1, max_length=200)


@router.post("/batch")
async def batch_disconnect(body: LeaseBatch, request: Request, user=USER):
    results = []
    for lease_id in dict.fromkeys(body.lease_ids):
        try:
            result = await disconnect_one(lease_id, request)
        except HTTPException as exc:
            if exc.status_code == 401:
                raise
            result = {"lease_id": lease_id, "ok": False, "error": "forbidden"}
        results.append(result)
    ok = sum(bool(r["ok"]) for r in results)
    return {"results": results, "summary": {"total": len(results), "ok": ok, "failed": len(results) - ok}}
