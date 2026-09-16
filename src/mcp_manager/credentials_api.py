"""Personal credential status and update endpoints."""
import copy

from fastapi import APIRouter, Depends, HTTPException, Request

from .catalog import restore
from .catalog_api import permitted
from .credentials import config_isolation
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


def personal_authorizer(request, server_id: str, revision: int):
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
        return user

    return authorize


@router.get("/mcps/{server_id}/credentials/status")
async def credential_status(server_id: str, request: Request, user=USER):
    row = await permitted(request, server_id, user)
    return await request.app.state.credentials.status(row, user.id)


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
        if saved_type not in {"bearer", "oauth"}:
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
    return await state.credentials.status(current, actor.id)
