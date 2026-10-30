"""Bounded authoritative completion receipts for owner-scoped logical operations."""
import asyncio
import time

from fastapi import HTTPException
from jsonschema import ValidationError
from sqlalchemy.orm import load_only

from .catalog_operations import current_operation
from .credentials import catalog_owner
from .database import McpServer, now
from .diagnostics import safe_gateway_error
from .runtime import GatewayError
from .storage_async import run_blocking


def clean_health(value):
    return (value.get("runtime_status") in {None, "stopped"} and not any(value.get(key) for key in (
        "startup_failure_count", "invalidated", "cache_error", "cache_error_code", "cache_stale",
        "last_startup_error", "last_startup_error_code", "last_startup_failure_at", "failed_gateway_names")))


async def begin_operation(catalog, scope):
    async with catalog.db.locked() as session:
        await catalog.index._validate_scope(session, scope)
        control = await catalog.index._control(session, scope, create=True)
        return issue_ticket(control)


def issue_ticket(control):
    control.result_sequence += 1
    return {"sequence": control.result_sequence, "epoch": control.epoch,
            "fence": control.refresh_fence, "success": control.success_sequence,
            "expires_at": time.time() + 3600}


async def bind_operation(catalog, row, user_id):
    # Resolve credentials and issue the fence within one primary transaction.
    # The previous path read the same authority once before, then again inside
    # the write lock. Neither a scope nor a receipt is trusted from process RAM.
    async with catalog.db.locked() as session:
        current = await session.get(McpServer, row.id, options=[load_only(
            McpServer.id, McpServer.revision, McpServer.isolation, McpServer.mode)])
        if not current or current.revision != row.revision:
            raise GatewayError("revision_changed", "MCP configuration changed")
        if current.mode == "disabled":
            raise GatewayError("disabled", "MCP service is disabled")
        scope = await catalog._index_scope(session, current, catalog_owner(current.isolation, user_id))
        control = await catalog.index._control(session, scope, create=True)
        catalog._remember_health(scope, dict(control.health or {}))
        return scope, issue_ticket(control)


async def record_result(catalog, row, user_id=None, *, error=None, kind="call",
                        scope=None, expected_refresh=None, ticket=None, operation_id=None):
    if isinstance(error, (asyncio.CancelledError, ValidationError)):
        return
    if isinstance(error, HTTPException) and error.status_code in {401, 403, 404, 409, 422}:
        return
    if error is not None:
        # A returned tool error, rejected request, timeout or malformed result
        # does not prove the MCP is unavailable. Count only connection/startup
        # failures, or discovery failures while actually refreshing a directory.
        code = getattr(error, "code", None)
        unavailable = code in {"connection_error", "startup_failed", "startup_timeout"}
        unavailable |= code == "outcome_unknown" and getattr(error, "details", {}).get("connection_failed") is True
        unavailable |= kind in {"refresh", "startup"} and code in {"discovery_failed", "discovery_timeout", "stopped"}
        if not unavailable and code not in {"auth_required", "oauth_failed", "cache_write_failed"}:
            return
        config = await catalog.credentials.materialize(row, user_id, require=False)
        error = safe_gateway_error(error, config=config)
        if error.code == "auth_required" and config.get("auth", {}).get("type") == "oauth":
            error.args = ("OAuth 授权已失效，请重新授权",)
        if error.code in {"revision_changed", "permission_revoked", "forbidden", "access_denied",
                          "lease_expired", "disabled", "shutting_down", "generation_superseded",
                          "tool_not_found", "invalid_arguments", "credential_changed", "cache_capacity",
                          "inventory_rebuilding", "storage_busy", "staging_capacity", "storage_low_space",
                          "index_capacity", "durable_capacity", "wal_pressure"}:
            return
    operation = current_operation.get()
    if operation is not None and operation.row is not None and operation.row.id == row.id:
        ticket = ticket or operation.ticket
        operation_id = operation_id or operation.identity
        scope = scope or operation.scope
    if scope is None:
        scope = await catalog.index_scope(row, user_id)
    if ticket is None:
        ticket = await begin_operation(catalog, scope)
    value = await run_blocking(catalog.cached, row, user_id)
    candidate = (error is None and kind == "call" and value.get("cache_status") == "ready"
                 and bool(value.get("cache_at")) and clean_health(value))
    generation, refresh = None, None
    if not candidate:
        async with catalog.index.db.session() as derived:
            generation = await catalog.index._current(derived, scope)
            refresh = await catalog.index.refresh_version(scope)
    if error is not None and operation is None and expected_refresh is not None and refresh != expected_refresh:
        return
    invalidated = False
    async with catalog.db.locked() as session:
        current = await session.get(McpServer, row.id, options=[load_only(
            McpServer.id, McpServer.revision, McpServer.isolation, McpServer.mode)])
        if not current or current.revision != row.revision or current.mode == "disabled":
            return
        actual = await catalog._index_scope(session, current, catalog_owner(current.isolation, user_id))
        if actual != scope:
            return
        control = await catalog.index._control(session, scope, create=True)
        receipt = dict(control.receipt or {})
        sequence = ticket["sequence"]
        completed = list(receipt.get("completed", []))
        if (ticket["expires_at"] <= time.time() or ticket["epoch"] != control.epoch
                or ticket["success"] != control.success_sequence
                or ticket["fence"] != control.refresh_fence
                or sequence <= receipt.get("floor", 0) or sequence in completed):
            return
        completed.append(sequence)
        completed.sort()
        if len(completed) > 256:
            receipt["floor"] = max(receipt.get("floor", 0), completed[-257])
            completed = completed[-256:]
        receipt["completed"] = completed
        health = dict(control.health or {})
        projection_unchanged = candidate and clean_health(health)
        own_publication = receipt.get("refresh", {}).get("operation_id") == operation_id and operation_id is not None
        if error is None:
            # A nested refresh already committed success. Replaying its outer
            # completion cannot erase a failure committed in the meantime.
            if not own_publication and sequence >= health.get("failure_sequence", 0):
                health = {key: health.get(key, value.get(key)) for key in (
                    "last_refresh_error", "last_refresh_error_code", "last_refresh_failure_at")
                    if kind != "refresh" and health.get(key, value.get(key)) is not None}
                control.success_sequence += 1
        elif error.code in {"auth_required", "oauth_failed", "cache_write_failed"}:
            # Owner authorization and local cache persistence need diagnostics,
            # but do not prove the MCP died or consume its availability budget.
            health.update(cache_error=str(error)[:500], cache_error_code=error.code,
                          cache_stale=generation is not None)
            if kind == "refresh":
                health.update(last_refresh_error=str(error)[:500], last_refresh_error_code=error.code,
                              last_refresh_failure_at=now().isoformat())
        else:
            count = health.get("startup_failure_count", 0) + 1
            stamp = now().isoformat()
            invalidated = count >= catalog.startup_failure_threshold
            details = {"startup_failure_count": count,
                       "runtime_status": "failed" if invalidated else "stopped",
                       "last_startup_error_code": error.code, "last_startup_error": str(error)[:500],
                       "last_startup_failure_at": stamp,
                       "cache_error": str(error)[:500], "cache_error_code": error.code,
                       "cache_stale": generation is not None and not invalidated,
                       "failure_sequence": sequence, "invalidated": invalidated}
            if kind == "refresh":
                details.update(last_refresh_error=str(error)[:500], last_refresh_error_code=error.code,
                               last_refresh_failure_at=stamp)
            health.update(details)
            if invalidated:
                control.epoch += 1
                receipt.pop("refresh", None)
        control.health, control.receipt, control.updated_at = health, receipt, now()
        version = control.success_sequence
    if operation is not None:
        operation.success_version = version
    catalog._remember_health(scope, health)
    if projection_unchanged:
        # The primary success sequence and bounded completion receipt are
        # committed even on this path. Only the unchanged disposable view is
        # skipped; concurrent/late failures keep the same fencing semantics.
        return
    if candidate:
        async with catalog.index.db.session() as derived:
            generation = await catalog.index._current(derived, scope)
    if error is not None:
        failed_refresh = operation.refresh_version if operation is not None else expected_refresh
        await catalog.index.project_failure(scope, failed_refresh or refresh, {
            key: item for key, item in health.items()
            if key not in {"invalidated", "failure_sequence"}
        })
    if generation is not None and not invalidated:
        value.update(cache_status="ready", cache_at=generation.published_at.isoformat(),
                     **{key[:-1] + "_count": (generation.counts or {}).get(key, 0)
                        for key in ("tools", "resources", "prompts", "templates")})
    from .catalog import FAILURE_DEFAULTS
    value.update(FAILURE_DEFAULTS, cache_error=None, cache_error_code=None, cache_stale=False)
    value.update({key: item for key, item in health.items() if key not in {"invalidated", "failure_sequence"}})
    if invalidated:
        value.update(cache_status="error", cache_at=None, cache_stale=False,
                     **{key: [] for key in ("tools", "resources", "prompts", "templates")},
                     **{key: 0 for key in ("tool_count", "resource_count", "prompt_count", "template_count")})
    elif error is not None and generation is None:
        value["cache_status"] = "auth_required" if error.code == "auth_required" else "error"
    try:
        await run_blocking(catalog.save_cache, row, value, user_id, content_changed=invalidated)
    except GatewayError as projection_error:
        if projection_error.code not in {
            "cache_capacity", "staging_capacity", "storage_low_space", "index_capacity",
            "durable_capacity", "wal_pressure", "storage_busy", "inventory_rebuilding", "cache_write_failed",
        }:
            raise
        # The authoritative receipt is already committed. A disposable
        # projection cannot replace a completed business outcome with a retry.
        import logging
        logging.getLogger(__name__).warning("Compatibility projection deferred: %s", projection_error.code)
    if error is not None:
        error.details.update(catalog.failure_details(row, user_id))
