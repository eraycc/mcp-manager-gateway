"""Agent MCP proposals and administrator approval workflow."""
import asyncio
import json
import math
from copy import deepcopy
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from .catalog import masked, restore, validate_slug, web_authorizer
from .database import McpProposal, McpServer, now
from .identity import admin_user
from .runtime import GatewayError, ServerSpec
from .transports import validate_service_config

router = APIRouter(prefix="/api/v1/mcp-proposals")
ADMIN = Depends(admin_user)
APPROVAL_FIELDS = {"mode", "isolation", "config_isolation"}
OAUTH_TEST_MESSAGE = "OAuth 类型需要在审批通过并授权后验证"


def validate_proposal_oauth(auth):
    if auth.get("type") != "oauth":
        return
    for field in ("authorization_url", "token_url", "client_id", "client_secret"):
        if not isinstance(auth.get(field), str) or not auth[field].strip():
            raise HTTPException(422, "OAuth " + field + " is required")


class ProposedMcp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    slug: str | None = Field(default=None, min_length=1, max_length=64)
    description: str = Field(default="", max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=100)
    transport: Literal["stdio", "streamable-http", "http", "sse", "rest"]
    config: dict
    source: str = Field(default="agent", max_length=128)
    purpose: str = Field(default="", max_length=4000)
    declared_capabilities: list[str] = Field(default_factory=list, max_length=100)
    requested_permissions: list[str] = Field(default_factory=list, max_length=100)


def _unwrap_json_strings(value: dict) -> dict:
    """Restore dict/list fields that the host MCP client flattened to JSON strings."""
    if not isinstance(value, dict):
        return value
    unwrapped: dict = {}
    for k, v in value.items():
        if isinstance(v, str) and v.startswith(("{", "[")):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, (dict, list)):
                    v = parsed
            except json.JSONDecodeError:
                pass
        unwrapped[k] = v
    return unwrapped


def normalize_proposals(value: dict) -> list[dict]:
    if not isinstance(value, dict):
        raise ValueError("MCP 提议必须是 JSON 对象")  # noqa: TRY004
    if APPROVAL_FIELDS & value.keys():
        raise ValueError("启动策略、实例隔离和 OAuth 配置隔离由审批人设置")
    raw = value.get("proposals")
    if raw is None:
        raw = [value]
    elif set(value) != {"proposals"}:
        raise ValueError("批量提交只能包含 proposals 字段")
    if isinstance(raw, str) and raw.startswith("["):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            pass
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise ValueError("proposals 必须包含 1-100 条记录")
    result = []
    for item in raw:
        if isinstance(item, dict):
            item = _unwrap_json_strings(item)
            if APPROVAL_FIELDS & item.keys():
                raise ValueError("启动策略、实例隔离和 OAuth 配置隔离由审批人设置")
        try:
            proposal = ProposedMcp.model_validate(item)
        except PydanticValidationError as exc:
            raise ValueError(str(exc)) from None
        data = proposal.model_dump()
        if data["transport"] == "http":
            data["transport"] = "streamable-http"
        if "config_isolation" in data["config"].get("auth", {}):
            raise ValueError("OAuth 配置隔离由审批人设置")
        validate_service_config(data["transport"], "service", data["config"])
        result.append(data)
    return result


async def list_proposals(app, user, *, q="", status="", page=1, page_size=20):
    """Read-only listing of MCP proposals with status filter, paging and per-status counts.

    Returns a trimmed, agent-friendly view: per-status counts plus a paged list of
    metadata fields. The full config payload (which may contain credentials) is
    intentionally excluded. Permission mirrors submit_proposals (admin only).
    """
    if user.role != "admin":
        raise HTTPException(403, "Administrator required to list MCP proposals")
    if page < 1 or not 1 <= page_size <= 200:
        raise HTTPException(422, "Invalid pagination")
    if status not in {"", "pending", "incomplete", "approved", "rejected"}:
        raise HTTPException(422, "Invalid status")
    async with app.state.db.session() as session:
        rows = list((await session.scalars(select(McpProposal).order_by(
            McpProposal.created_at.desc(), McpProposal.id
        ))).all())
    counts = {key: 0 for key in ("pending", "incomplete", "approved", "rejected")}
    for row in rows:
        if row.status in counts:
            counts[row.status] += 1
    catalog = app.state.catalog
    query = q.lower()

    def name_of(row):
        try:
            return catalog.unseal(row.payload).get("name", "")
        except Exception:  # noqa: BLE001 - one corrupt proposal must not break listing
            return ""

    haystack = lambda row: (name_of(row) + " " + (row.purpose or "") + " " + (row.source or "")).lower()
    filtered = [row for row in rows
                if (not status or row.status == status)
                and (not query or query in haystack(row))]
    total = len(filtered)
    page_rows = filtered[(page - 1) * page_size: page * page_size]
    return {
        "total": total,
        "counts": counts,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if total else 0,
        "items": [
            {
                "id": row.id, "name": name_of(row), "status": row.status,
                "purpose": row.purpose or "", "source": row.source or "",
                "created_at": row.created_at.isoformat() if row.created_at else "",
                "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                **({"rejection_reason": row.rejection_reason or ""}
                   if row.status == "rejected" else {}),
            }
            for row in page_rows
        ],
    }


def resource_index(rows, cached, *, mcp="", keyword=""):
    mcp_key, query = mcp.strip().lower(), keyword.strip().lower()
    result = []
    for row in rows:
        provider_text = f"{row.id} {row.name} {row.slug}".lower()
        if mcp_key and mcp_key != "*" and mcp_key not in provider_text:
            continue
        provider = {"id": row.id, "name": row.name, "slug": row.slug}
        data = cached(row)
        for kind, key in (("resource", "resources"), ("template", "templates"), ("prompt", "prompts")):
            for original in data.get(key, []):
                item = deepcopy(original)
                raw_uri = item.get("uri") if kind == "resource" else item.get("uriTemplate")
                if raw_uri:
                    public_uri = "mcp-manager://" + row.id + "/" + raw_uri
                    item["uri" if kind == "resource" else "uriTemplate"] = public_uri
                haystack = json.dumps(item, ensure_ascii=False).lower() + " " + provider_text
                if query and query not in haystack:
                    continue
                item.update(kind=kind, mcp=provider)
                if kind == "resource":
                    item["read_with"] = {
                        "tool": "gateway_read_resource",
                        "arguments": {"uri": item["uri"]},
                    }
                result.append(item)
    return result


def proposal_public(row, catalog, *, detail=False, reveal=False):
    payload = catalog.unseal(row.payload)
    result = {
        "id": row.id,
        "user_id": row.user_id,
        "token_id": row.token_id,
        "source": row.source,
        "purpose": row.purpose,
        "declared_capabilities": row.declared_capabilities,
        "requested_permissions": row.requested_permissions,
        "name": payload["name"],
        "transport": payload["transport"],
        "auth_type": payload.get("config", {}).get("auth", {}).get("type", "none"),
        "status": row.status,
        "mode": row.mode,
        "isolation": row.isolation,
        "config_isolation": row.config_isolation,
        "rejection_reason": row.rejection_reason,
        "approved_mcp_id": row.approved_mcp_id,
        "test_status": row.test_status,
        "test_result": row.test_result or {},
        "test_error": row.test_error or "",
        "tested_at": row.tested_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if detail:
        result["payload"] = payload if reveal else masked(payload)
    return result


async def submit_proposals(app, user, token, value):
    if not token or not token.enable_mcp_proposal or user.role != "admin":
        raise HTTPException(403, "This administrator token cannot propose MCP services")
    items = normalize_proposals(value)
    rows = []
    async with app.state.db.locked() as session:
        for item in items:
            payload = {key: item[key] for key in ("name", "slug", "description", "tags", "transport", "config")}
            row = McpProposal(
                user_id=user.id,
                token_id=token.id,
                source=item["source"],
                purpose=item["purpose"],
                declared_capabilities=item["declared_capabilities"],
                requested_permissions=item["requested_permissions"],
                payload=app.state.catalog.seal(payload),
                status="pending",
            )
            session.add(row)
            rows.append(row)
        await session.flush()
        result = [{"id": row.id, "name": app.state.catalog.unseal(row.payload)["name"], "status": row.status}
                  for row in rows]
    return {"items": result, "message": "MCP 提议已提交，请前往控制面板审批"}


@router.get("")
async def listing(request: Request, q: str = "", status: str = "", page: int = 1,
                  page_size: int = 20, user=ADMIN):
    if page < 1 or not 1 <= page_size <= 200:
        raise HTTPException(422, "Invalid pagination")
    allowed = {"", "pending", "incomplete", "approved", "rejected"}
    if status not in allowed:
        raise HTTPException(422, "Invalid proposal status")
    statement = select(McpProposal)
    if status:
        statement = statement.where(McpProposal.status == status)
    db = request.app.state.db
    async with db.session() as session:
        rows = list((await session.scalars(statement.order_by(
            McpProposal.created_at.desc(), McpProposal.id
        ))).all())
    if q:
        query = q.lower()
        rows = [row for row in rows if query in (
            request.app.state.catalog.unseal(row.payload)["name"] + " " + row.purpose + " " + row.source
        ).lower()]
    total = len(rows)
    rows = rows[(page - 1) * page_size:page * page_size]
    return {
        "items": [proposal_public(row, request.app.state.catalog) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size),
    }


@router.get("/{proposal_id}")
async def detail(proposal_id: str, request: Request, response: Response,
                 reveal: bool = False, user=ADMIN):
    async with request.app.state.db.session() as session:
        row = await session.get(McpProposal, proposal_id)
    if not row:
        raise HTTPException(404, "Proposal not found")
    if reveal:
        await web_authorizer(request)()
        response.headers["Cache-Control"] = "no-store"
    return proposal_public(
        row, request.app.state.catalog, detail=True, reveal=reveal
    )


async def _configure(row, values, catalog, session=None):
    payload = catalog.unseal(row.payload)
    for key in ("name", "slug", "description", "tags", "transport"):
        if key in values:
            payload[key] = values[key]
    slug = payload.get("slug")
    if slug:
        try:
            validate_slug(slug)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if session is not None:
            duplicate = await session.scalar(
                select(McpServer.id).where(McpServer.slug == slug)
            )
            if duplicate:
                raise HTTPException(409, "MCP slug already exists")
    if "config" in values:
        payload["config"] = restore(values["config"], payload["config"])
    if APPROVAL_FIELDS & values.keys():
        if values.get("mode") is not None:
            row.mode = values["mode"]
        if values.get("isolation") is not None:
            row.isolation = values["isolation"]
        if "config_isolation" in values:
            row.config_isolation = values["config_isolation"]
    mode = row.mode
    isolation = row.isolation
    if mode is not None and mode not in {"lazy", "eager", "disabled"}:
        raise HTTPException(422, "Invalid mode")
    if isolation is not None and isolation not in {"service", "user", "session"}:
        raise HTTPException(422, "Invalid isolation")
    if row.config_isolation is not None and row.config_isolation not in {"shared", "user"}:
        raise HTTPException(422, "Invalid OAuth config isolation")
    if payload["transport"] != "stdio" and isolation == "session":
        raise HTTPException(422, "Session isolation is stdio-only")
    auth = payload["config"].get("auth", {})
    if auth.get("type") == "oauth":
        validate_proposal_oauth(auth)
        if row.config_isolation:
            auth["config_isolation"] = row.config_isolation
        else:
            auth.pop("config_isolation", None)
    validate_service_config(payload["transport"], isolation or "service", payload["config"])
    row.payload = catalog.seal(payload)
    row.status = "pending" if mode and isolation else "incomplete"
    row.updated_at = now()


@router.patch("/{proposal_id}")
async def configure(proposal_id: str, data: dict, request: Request, user=ADMIN):
    async with request.app.state.db.locked() as session:
        await web_authorizer(request)(session)
        row = await session.get(McpProposal, proposal_id)
        if not row:
            raise HTTPException(404, "Proposal not found")
        if row.status in {"approved", "rejected"}:
            raise HTTPException(409, "Finalized proposal cannot be edited")
        await _configure(row, data, request.app.state.catalog, session)
        await session.flush()
        return proposal_public(row, request.app.state.catalog, detail=True)


async def _persist_test_result(request, proposal_id, status, *, result=None, error=""):
    async with request.app.state.db.locked() as session:
        row = await session.get(McpProposal, proposal_id)
        if not row:
            raise HTTPException(404, "Proposal not found")
        row.test_status = status
        row.test_result = result or {}
        row.test_error = error[:4000]
        if status != "pending":
            row.tested_at = now()
        row.updated_at = now()


async def run_proposal_test(request, proposal_id, user):
    actor = await web_authorizer(request)()
    catalog = request.app.state.catalog
    async with request.app.state.db.session() as session:
        row = await session.get(McpProposal, proposal_id)
    if not row:
        raise HTTPException(404, "Proposal not found")
    payload = catalog.unseal(row.payload)
    isolation = row.isolation or "service"
    if payload["config"].get("auth", {}).get("type") == "oauth":
        raise HTTPException(409, OAUTH_TEST_MESSAGE)
    await _persist_test_result(request, proposal_id, "pending")
    runtime = request.app.state.runtime
    lease = None
    try:
        validate_service_config(payload["transport"], isolation, payload["config"])
        lease = runtime.create_lease(actor.id, "diagnostic", kind="maintenance")
        spec = ServerSpec(
            "diagnose-proposal-" + uuid4().hex,
            payload["transport"],
            payload["config"],
            "lazy",
            isolation,
        )
        discovered = await runtime.discover(spec, lease.id)
        result = {
            "tool_count": len(discovered.get("tools", [])),
            "resource_count": len(discovered.get("resources", [])),
            "prompt_count": len(discovered.get("prompts", [])),
            "template_count": len(discovered.get("templates", [])),
        }
        await _persist_test_result(request, proposal_id, "success", result=result)
        return {"ok": True, **result, "capabilities": discovered}
    except asyncio.CancelledError:
        await asyncio.shield(_persist_test_result(
            request, proposal_id, "cancelled", error="Operation cancelled"
        ))
        raise
    except Exception as exc:
        error = str(getattr(exc, "detail", exc)) or type(exc).__name__
        await _persist_test_result(request, proposal_id, "failed", error=error)
        raise
    finally:
        if lease:
            await runtime.release(lease.id)


@router.post("/{proposal_id}/test")
async def test_proposal(proposal_id: str, request: Request, user=ADMIN):
    return await run_proposal_test(request, proposal_id, user)


@router.post("/batch-test")
async def batch_test(data: dict, request: Request, user=ADMIN):
    await web_authorizer(request)()
    ids = list(dict.fromkeys(data.get("ids", [])))
    if not 1 <= len(ids) <= 500:
        raise HTTPException(422, "Select between 1 and 500 proposals")

    async def operation(proposal_id):
        return await run_proposal_test(request, proposal_id, user)

    async def cancelled(proposal_id):
        await _persist_test_result(
            request, proposal_id, "cancelled", error="Operation cancelled"
        )

    return request.app.state.jobs.submit(
        "mcp.proposal.test", ids, operation, user.id, cancel_operation=cancelled
    )


@router.post("/batch")
async def batch(data: dict, request: Request, user=ADMIN):
    ids = list(dict.fromkeys(data.get("ids", [])))
    if not 1 <= len(ids) <= 500:
        raise HTTPException(422, "Select between 1 and 500 proposals")
    action = data.get("action")
    if action not in {"configure", "approve", "reject", "delete"}:
        raise HTTPException(422, "Unsupported batch action")
    results = []
    for proposal_id in ids:
        try:
            if action == "approve":
                result = await approve(proposal_id, {}, request, user)
            elif action == "reject":
                result = await reject(proposal_id, {"reason": data.get("reason", "")}, request, user)
            elif action == "delete":
                result = await remove(proposal_id, request, user)
            else:
                result = await configure(proposal_id, data.get("configuration", {}), request, user)
            results.append({"id": proposal_id, "ok": True, "result": result})
        except (HTTPException, ValueError, GatewayError) as exc:
            results.append({"id": proposal_id, "ok": False, "error": str(getattr(exc, "detail", exc))})
    return {"results": results}


@router.post("/{proposal_id}/approve")
async def approve(proposal_id: str, data: dict, request: Request, user=ADMIN):
    await web_authorizer(request)()
    db, catalog = request.app.state.db, request.app.state.catalog
    async with db.session() as session:
        row = await session.get(McpProposal, proposal_id)
        if not row:
            raise HTTPException(404, "Proposal not found")
        if row.status == "approved":
            return proposal_public(row, catalog)
        if row.status == "rejected":
            raise HTTPException(409, "Rejected proposal cannot be approved")
        payload = catalog.unseal(row.payload)
        mode = data.get("mode") or row.mode or "lazy"
        isolation = data.get("isolation") or row.isolation or "service"
        config_isolation = data.get("config_isolation") or row.config_isolation
    config = deepcopy(payload["config"])
    if config.get("auth", {}).get("type") == "oauth":
        validate_proposal_oauth(config["auth"])
        if config_isolation:
            config["auth"]["config_isolation"] = config_isolation
        else:
            config["auth"].pop("config_isolation", None)
    canonical = json.dumps(
        {"transport": payload["transport"], "config": config, "isolation": isolation},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    for existing in await catalog.rows():
        existing_config = await catalog.credentials.materialize(existing, user.id, require=False)
        candidate = json.dumps(
            {"transport": existing.transport, "config": existing_config, "isolation": existing.isolation},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        if candidate == canonical:
            raise HTTPException(409, "An identical MCP service already exists")
    created = await catalog.create(
        {**payload, "config": config, "mode": mode, "isolation": isolation},
        authorize=web_authorizer(request),
        user_id=user.id,
    )
    async with db.locked() as session:
        await web_authorizer(request)(session)
        row = await session.get(McpProposal, proposal_id)
        if row.status in {"approved", "rejected"}:
            raise HTTPException(409, "Proposal was finalized concurrently")
        row.status = "approved"
        row.mode = mode
        row.isolation = isolation
        row.config_isolation = config_isolation
        row.approved_mcp_id = created.id
        row.updated_at = now()
        await session.flush()
        result = proposal_public(row, catalog)
    await request.app.state.logs.audit("mcp.proposal.approve", user.id, {
        "proposal_id": proposal_id, "mcp_id": created.id,
    })
    return result


@router.post("/{proposal_id}/reject")
async def reject(proposal_id: str, data: dict, request: Request, user=ADMIN):
    reason = str(data.get("reason", "")).strip()
    if not reason:
        raise HTTPException(422, "Rejection reason is required")
    async with request.app.state.db.locked() as session:
        await web_authorizer(request)(session)
        row = await session.get(McpProposal, proposal_id)
        if not row:
            raise HTTPException(404, "Proposal not found")
        if row.status == "approved":
            raise HTTPException(409, "Approved proposal cannot be rejected")
        row.status = "rejected"
        row.rejection_reason = reason[:4000]
        row.updated_at = now()
        await session.flush()
        return proposal_public(row, request.app.state.catalog)


@router.delete("/{proposal_id}")
async def remove(proposal_id: str, request: Request, user=ADMIN):
    async with request.app.state.db.locked() as session:
        await web_authorizer(request)(session)
        row = await session.get(McpProposal, proposal_id)
        if not row:
            raise HTTPException(404, "Proposal not found")
        await session.delete(row)
    return {"ok": True}
