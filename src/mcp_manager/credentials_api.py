"""Personal credential status and update endpoints."""
import copy
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .catalog import restore
from .catalog_api import permitted
from .credentials import PERSONAL_AUTH_TYPES, STATIC_AUTH_FIELDS, config_isolation, static_auth_configured
from .database import McpServer
from .identity import current_user, revalidate_user
from .runtime import GatewayError
from .transports import validate_service_config

router = APIRouter(prefix="/api/v1")
USER = Depends(current_user)


def contains_redacted(value) -> bool:
    if value == "[REDACTED]":
        return True
    if isinstance(value, dict):
        return any(contains_redacted(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_redacted(item) for item in value)
    return False


HTTP_TRANSPORTS = frozenset({"streamable-http", "sse", "rest"})


class PersonalRefresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(strict=True, ge=1)


async def personal_credential_status(state, row, user_id, *, session=None):
    status = await state.credentials.status(row, user_id, session=session)
    supported = row.transport in HTTP_TRANSPORTS and status["required"]
    allowed = supported and row.mode != "disabled" and status["state"] not in {
        "missing", "pending_authorization", "not_required",
    }
    reason = ""
    if not supported:
        reason = "仅支持按用户隔离且需要认证的 HTTP 类服务"
    elif row.mode == "disabled":
        reason = "此服务已禁用"
    elif status["state"] == "missing":
        reason = ("请联系管理员完善 OAuth 共享配置" if status["auth_type"] == "oauth"
                  and status["config_isolation"] == "shared" else "请先保存个人凭据")
    elif status["state"] == "pending_authorization":
        reason = "请先完成本人 OAuth 授权"
    elif status["auth_type"] == "oauth":
        from .oauth import token_expiry

        personal = await state.credentials.load(row.id, user_id, session=session)
        token = personal.get("oauth_token", {})
        if token_expiry(token) <= time.time() + 60 and not token.get("refresh_token"):
            allowed, reason = False, "本人 OAuth 授权已过期，请重新授权"
    return status | {
        "revision": row.revision, "refresh_supported": supported,
        "refresh_allowed": allowed, "refresh_reason": reason,
    }


def personal_authorizer(request, server_id: str, revision: int, *, refresh=False):
    """Revalidate user assignment, revision, and isolation at dispatch time."""

    async def authorize(session=None):
        if session is None:
            async with request.app.state.db.locked() as current:
                return await authorize(current)
        user = await revalidate_user(request, session)
        row = await session.get(McpServer, server_id, populate_existing=True)
        if not row:
            raise HTTPException(404, "MCP not found")
        if row.revision != revision:
            raise HTTPException(409, "MCP configuration changed; reload before saving")
        if row.isolation != "user":
            raise HTTPException(422, "Personal credentials require user isolation")
        if (
            user.role != "admin"
            and user.scope_mode != "all"
            and server_id not in user.mcp_ids
        ):
            raise HTTPException(404, "MCP not found")
        if refresh:
            status = await personal_credential_status(request.app.state, row, user.id, session=session)
            if not status["refresh_supported"]:
                raise HTTPException(422, status["refresh_reason"])
            if row.mode == "disabled":
                raise GatewayError("disabled", "MCP service is disabled")
            if not status["refresh_allowed"]:
                raise GatewayError("auth_required", status["refresh_reason"])
        return user

    return authorize


@router.post("/mcps/credentials/status-batch")
async def credential_status_batch(data: dict, request: Request, user=USER):
    ids = data.get("ids")
    if not isinstance(ids, list) or len(ids) > 50 or any(not isinstance(item, str) or not item for item in ids):
        raise HTTPException(422, "ids must contain at most 50 non-empty strings")
    requested = list(dict.fromkeys(ids))
    async with request.app.state.db.locked() as session:
        actor = await revalidate_user(request, session)
        allowed = requested
        if actor.role != "admin" and actor.scope_mode != "all":
            permitted_ids = set(actor.mcp_ids)
            allowed = [item for item in requested if item in permitted_ids]
        rows = (await session.scalars(select(McpServer).where(McpServer.id.in_(allowed)))).all() if allowed else []
        by_id = {row.id: row for row in rows}
        items = []
        for identity in allowed:
            row = by_id.get(identity)
            if row is not None:
                status = await request.app.state.credentials.status(row, actor.id, session=session)
                items.append({"id": identity, **status})
    return {"items": items}


@router.get("/mcps/{server_id}/credentials/status")
async def credential_status(server_id: str, request: Request, user=USER):
    row = await permitted(request, server_id, user)
    return await personal_credential_status(request.app.state, row, user.id)


@router.put("/mcps/{server_id}/credentials")
async def save_credentials(server_id: str, data: dict, request: Request, user=USER):
    state = request.app.state
    row = await permitted(request, server_id, user)
    revision = data.get("revision")
    if not isinstance(revision, int):
        raise HTTPException(422, "revision is required")
    submitted = data.get("auth")
    if not isinstance(submitted, dict):
        raise HTTPException(422, "auth is required")

    authorize = personal_authorizer(request, server_id, revision)
    async with state.db.locked() as session:
        actor = await authorize(session)
        row = await session.get(McpServer, server_id, populate_existing=True)
        descriptor = state.catalog.unseal(row.config)
        saved_type = descriptor.get("auth", {}).get("type", "none")
        if saved_type not in PERSONAL_AUTH_TYPES:
            raise HTTPException(422, "This MCP does not use personal credentials")
        if submitted.get("type") != saved_type:
            raise HTTPException(422, "Authentication type cannot be changed here")
        if saved_type == "oauth" and config_isolation(descriptor) != "user":
            raise HTTPException(422, "Shared OAuth configuration is managed by an administrator")

        previous = await state.credentials.materialize(
            row, actor.id, session=session, require=False
        )
        resolved_auth = restore(submitted, previous.get("auth", {}))
        if contains_redacted(resolved_auth):
            raise GatewayError(
                "auth_required", "Saved credential is unavailable; enter it again"
            )
        if saved_type in STATIC_AUTH_FIELDS and not static_auth_configured(saved_type, resolved_auth):
            raise HTTPException(422, "Complete all required credential fields")
        full = copy.deepcopy(descriptor)
        full["auth"] = resolved_auth
        if saved_type == "oauth":
            full["auth"]["config_isolation"] = "user"
        validate_service_config(row.transport, row.isolation, full)
        await state.credentials.persist_config(
            session, row, full, row.isolation, actor.id
        )

    await state.runtime.invalidate_credential(server_id, actor.id)
    state.catalog.delete_cache(row, actor.id)
    if row.mode != "disabled":
        try:
            await state.catalog.refresh(
                server_id, actor.id, authorize=authorize
            )
        except (GatewayError, HTTPException):
            # The status endpoint exposes fail-closed discovery diagnostics.
            pass
    current = await state.catalog.get(server_id)
    return await personal_credential_status(state, current, actor.id)


@router.post("/mcps/{server_id}/credentials/refresh")
async def refresh_personal_capabilities(server_id: str, data: PersonalRefresh, request: Request, user=USER):
    state = request.app.state
    row = await permitted(request, server_id, user)
    authorize = personal_authorizer(request, server_id, data.revision, refresh=True)
    actor = await authorize()
    if state.catalog.unseal(row.config).get("auth", {}).get("type") == "oauth":
        # Complete any token renewal before capturing the catalog credential generation.
        scope = await state.catalog.index_scope(row, actor.id)
        expected_refresh = await state.catalog.index.refresh_version(scope)
        try:
            await state.catalog.spec(row, actor.id)
        except GatewayError as exc:
            raise await state.catalog.record_refresh_failure(
                row, exc, actor.id, scope=scope, expected_refresh=expected_refresh,
                had_ready=state.catalog.cached(row, actor.id)["cache_status"] == "ready",
            ) from exc
        await authorize()
    result = await state.catalog.refresh_index(server_id, actor.id, authorize=authorize)
    await authorize()
    current = await state.catalog.get(server_id)
    cache = state.catalog.cached(current, actor.id)
    await state.logs.audit("mcp.credentials.refresh", actor.id, {"mcp_id": server_id})
    return {
        **await personal_credential_status(state, current, actor.id),
        "cache_status": cache["cache_status"], "cache_at": cache.get("cache_at"),
        **{key: result[key] for key in ("tool_count", "resource_count", "prompt_count", "template_count")},
    }
