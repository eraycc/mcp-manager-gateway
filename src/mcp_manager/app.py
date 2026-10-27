"""FastAPI composition root and the single runtime owner."""
import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock, Timeout
from jsonschema import ValidationError
from sqlalchemy import select

from . import (
    audit_api,
    catalog_api,
    credentials_api,
    embedding_api,
    identity,
    leases_api,
    oauth_metadata,
    operations_api,
    proposals,
    storage_api,
)
from .audit_context import AuditContextMiddleware
from .catalog import Catalog
from .config import PACKAGE_ROOT, Settings
from .credentials import CredentialStore
from .database import ApiToken, Database, McpServer, User, get_setting, now
from .embeddings import EmbeddingIndex
from .job_store import JobStore
from .jobs import Jobs
from .logs import LogStore
from .oauth import OAuth
from .oauth import public_router as oauth_public_router
from .oauth import router as oauth_router
from .runtime import GatewayError, Lease, Runtime, RuntimeCapacity
from .storage_maintenance import StorageMaintenance
from .transports import connect, load_transport_plugins


async def _reap_runtime(app):
    state = app.state
    await state.runtime.reap()
    if hasattr(state, "gateway"):
        await state.gateway.reap()


async def batch_authorization_state(db, leases):
    """Load each unique identity once and derive every lease's current MCP scope."""
    relevant = [
        lease for lease in leases
        if lease.token_id not in {"maintenance", "test", "diagnostic"}
        and lease.user_id
        and lease.user_id != "anonymous"
    ]
    if not relevant:
        return {}
    user_ids = {lease.user_id for lease in relevant}
    token_ids = {lease.token_id for lease in relevant if lease.token_id}
    async with db.session() as session:
        users = {
            row.id: row
            for row in (
                await session.scalars(select(User).where(User.id.in_(user_ids)))
            ).all()
        }
        tokens = {
            row.id: row
            for row in (
                await session.scalars(select(ApiToken).where(ApiToken.id.in_(token_ids)))
            ).all()
        }
        enabled = set(
            (
                await session.scalars(
                    select(McpServer.id).where(McpServer.mode != "disabled")
                )
            ).all()
        )
    result = {}
    for lease in relevant:
        user = users.get(lease.user_id)
        token = tokens.get(lease.token_id)
        if (
            user is None
            or user.disabled
            or token is None
            or token.user_id != user.id
            or token.disabled
            or identity.expired(token.expires_at)
        ):
            result[lease.id] = None
            continue
        allowed = set(enabled)
        if user.role != "admin" and user.scope_mode != "all":
            allowed.intersection_update(user.mcp_ids)
        if token.scope_mode != "all":
            allowed.intersection_update(token.mcp_ids)
        result[lease.id] = allowed
    return result


async def _revoke_leases(app):
    """Revalidate active identities and immediately narrow revoked access."""
    state = app.state
    leases = list(state.runtime.leases.values())
    states = await batch_authorization_state(state.db, leases)
    for lease in leases:
        if lease.id not in states:
            continue
        allowed = states[lease.id]
        if allowed is None:
            await state.runtime.release(lease.id)
        else:
            await state.runtime.restrict_lease(lease.id, allowed)


async def _clean_instances(app):
    """Revalidate detached personal/session instances, including their token scope."""
    state = app.state
    instances = list(state.runtime.instances.values())
    ownership = {
        instance.key: (
            instance.spec.credential_owner
            if instance.spec.credential_owner not in {"", "service"}
            else instance.key[-1] if instance.spec.isolation == "user"
            else instance.admitted_user if instance.spec.isolation == "session"
            and instance.admitted_user != "anonymous"
            and not (instance.admitted_user == "system"
                     and instance.admitted_token in {"maintenance", "test", "diagnostic"}) else None
        )
        for instance in instances
    }
    owners = {owner for owner in ownership.values() if owner}
    async with state.db.session() as session:
        users = {
            row.id: row
            for row in (
                await session.scalars(select(User).where(User.id.in_(owners)))
            ).all()
        } if owners else {}
    session_instances = [i for i in instances if i.spec.isolation == "session"]
    authorization = await batch_authorization_state(state.db, [
        Lease(i.generation, i.admitted_user, i.admitted_token, None, 0)
        for i in session_instances
    ])
    anonymous_allowed = (await _anonymous_allowed(app)
                         if any(i.admitted_user == "anonymous" for i in session_instances) else set())
    for instance in instances:
        denied_session = False
        if instance.spec.isolation == "session":
            if instance.admitted_user == "anonymous":
                denied_session = instance.spec.id not in anonymous_allowed
            elif instance.generation in authorization:
                allowed = authorization[instance.generation]
                denied_session = allowed is None or instance.spec.id not in allowed
        owner = ownership[instance.key]
        if not owner:
            if denied_session:
                await state.runtime.stop_instance(instance, clear_refs=True)
            continue
        user = users.get(owner)
        denied = (
            not user or user.disabled
            or (
                user.role != "admin" and user.scope_mode != "all"
                and instance.spec.id not in user.mcp_ids
            )
        )
        if denied or denied_session:
            await state.runtime.stop_instance(instance, clear_refs=True)
            if instance.spec.credential_owner:
                state.runtime.credential_versions[(instance.spec.id, owner)] = None


async def _anonymous_allowed(app):
    state = app.state
    anonymous_allowed = set()
    if not await get_setting(state.db, "token_auth_enabled", True):
        anonymous_allowed = {
            row.id for row in await state.catalog.rows() if row.mode != "disabled"
        }
        if await get_setting(state.db, "anonymous_scope_mode", "selected") != "all":
            anonymous_allowed &= set(
                await get_setting(state.db, "anonymous_mcp_ids", [])
            )
    return anonymous_allowed


async def _restrict_anonymous(app):
    state = app.state
    anonymous_allowed = await _anonymous_allowed(app)
    for lease in list(state.runtime.leases.values()):
        if lease.user_id == "anonymous":
            await state.runtime.restrict_lease(lease.id, anonymous_allowed)


async def _scheduled_refresh(app):
    state = app.state
    if not await get_setting(state.db, "refresh_enabled", False):
        state.refresh_next = None
        return
    trigger = CronTrigger.from_crontab(
        await get_setting(state.db, "refresh_cron", "0 3 * * *"),
        timezone=await get_setting(state.db, "timezone", "Asia/Shanghai"),
    )
    current = now()
    if state.refresh_next is None:
        state.refresh_next = trigger.get_next_fire_time(None, current)
    elif current >= state.refresh_next:
        targets = await state.catalog.refresh_targets()
        await state.jobs.submit(
            "mcp.scheduled_refresh", targets, state.catalog.refresh_target
        )
        state.refresh_next = trigger.get_next_fire_time(state.refresh_next, current)


async def _build_embeddings(app):
    from .embedding_api import load_embedding_config

    await app.state.embeddings.build_pending(
        await load_embedding_config(app.state),
        generation_limit=1,
    )


async def _log_retention(app):
    state = app.state
    loop = asyncio.get_running_loop()
    if loop.time() < state.retention_next:
        return
    # Reserve before yielding so a concurrent settings update can request another pass.
    state.retention_next = loop.time() + 3600
    try:
        days = await get_setting(state.db, "log_retention_days", 0)
        if days:
            from datetime import timedelta

            cutoff = now() - timedelta(days=days, microseconds=1)
            await state.logs.delete(to_time=cutoff.isoformat())
        audit_days = await get_setting(state.db, "audit_log_retention_days", 0)
        if audit_days:
            await state.logs.audits.retain(audit_days, at=now())
        from .retention import run_startup_cache_cleanup, run_storage_retention

        if not state.cache_cleanup_done:
            await run_startup_cache_cleanup(state)
            state.cache_cleanup_done = True
        await run_storage_retention(state, at=now(), batch_limit=250)
    except Exception:
        state.retention_next = 0
        raise


MAINTENANCE_STEPS = (
    _reap_runtime,
    _revoke_leases,
    _clean_instances,
    _restrict_anonymous,
    _scheduled_refresh,
    _build_embeddings,
    _log_retention,
)


async def _maintenance_cycle(app):
    state = app.state
    for step in MAINTENANCE_STEPS:
        try:
            await step(app)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate maintenance steps
            with contextlib.suppress(Exception):
                await state.logs.audit(
                    "maintenance.error",
                    None,
                    {"step": step.__name__, "error_type": type(exc).__name__},
                )


async def maintenance(app):
    while True:
        await _maintenance_cycle(app)
        await asyncio.sleep(5)


def create_app(config=None):
    config = config or Settings()

    @asynccontextmanager
    async def lifespan(app):
        state = app.state
        lock = FileLock(str(Path(config.data_dir) / "runtime.lock"))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            raise RuntimeError("Another MCP Manager runtime already owns this data directory")
        state.db = Database(config)
        state.logs = LogStore(config.data_dir)
        state.password_pool = identity.PasswordWorkPool(
            max_workers=config.password_workers,
            max_waiters=config.password_waiters,
        )
        state.identity_rates = identity.IdentityRateLimiter(
            max_entries=config.identity_rate_limit_entries,
        )
        load_transport_plugins()
        state.runtime = Runtime(
            connect,
            capacity=RuntimeCapacity(
                global_calls=config.runtime_global_calls,
                user_calls=config.runtime_user_calls,
                mcp_calls=config.runtime_mcp_calls,
                queued_calls=config.runtime_queued_calls,
                global_instances=config.runtime_global_instances,
                user_instances=config.runtime_user_instances,
                per_instance_waiters=config.runtime_instance_waiters,
                queue_timeout=config.runtime_queue_timeout,
            ),
        )
        state.catalog = Catalog(state.db, state.runtime, state.logs, config)
        state.jobs = Jobs(store=JobStore(state.db, state.catalog.artifacts, signing_key=config.jwt_secret))
        state.catalog.jobs = state.jobs
        state.credentials = CredentialStore(state.catalog)
        state.catalog.credentials = state.credentials
        state.embeddings = EmbeddingIndex(
            config.data_dir,
            db=state.db,
            artifacts=state.catalog.artifacts,
        )
        state.oauth = OAuth(state.catalog, state.credentials)
        state.catalog.oauth = state.oauth
        state.runtime.on_starting = state.catalog.connection_starting
        state.runtime.on_connect = state.catalog.connection_ready
        state.runtime.on_start_failure = state.catalog.start_failed
        state.runtime.on_auth_failure = state.oauth.mark_reauth_spec
        state.refresh_next = None
        state.retention_next = 0
        state.cache_cleanup_done = False
        from .retention import cache_changed

        state.catalog.on_cache_change = lambda server_id: cache_changed(state, server_id)
        state.catalog.index.on_publish = state.catalog.on_cache_change
        task = None
        try:
            await state.db.initialize()
            state.storage = StorageMaintenance(state)
            await state.storage.startup()
            await state.jobs.start()
            state.cors_origins = await get_setting(state.db, "cors_origins", ["*"])
            state.allowed_hosts = await get_setting(state.db, "allowed_hosts", ["*"])
            from zoneinfo import ZoneInfo
            state.logs.timezone = ZoneInfo(await get_setting(state.db, "timezone", "Asia/Shanghai"))
            state.runtime.idle_seconds = await get_setting(state.db, "idle_seconds", 86400)
            state.runtime.connection_ttl_seconds = await get_setting(state.db, "connection_ttl_seconds", 43200)
            bridge_settings = operations_api.normalize_bridge_settings({
                "bridge_heartbeat_seconds": await get_setting(state.db, "bridge_heartbeat_seconds", 90),
                "bridge_missed_heartbeats": await get_setting(state.db, "bridge_missed_heartbeats", 3),
            })
            state.runtime.bridge_heartbeat_seconds = bridge_settings["bridge_heartbeat_seconds"]
            state.runtime.bridge_missed_heartbeats = bridge_settings["bridge_missed_heartbeats"]
            state.catalog.startup_failure_threshold = await get_setting(
                state.db, "startup_failure_threshold", 3
            )
            async with contextlib.AsyncExitStack() as stack:
                if hasattr(state, "protocol"):
                    await stack.enter_async_context(state.protocol.session_manager.run())
                eager = {
                    row.id
                    for row in await state.catalog.rows()
                    if row.mode == "eager"
                }
                rows = {row.id: row for row in await state.catalog.rows()}
                targets = [target for target in await state.catalog.refresh_targets()
                           if target["server_id"] in eager or state.catalog.cached(
                               rows[target["server_id"]], target["user_id"])["cache_status"] == "empty"]
                if targets:
                    await state.jobs.submit("mcp.startup", targets, state.catalog.warm_target)
                task = asyncio.create_task(maintenance(app))
                state.ready = True
                yield
                state.ready = False
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await state.jobs.close()
            await state.catalog.close()
            await state.runtime.close()
            await state.logs.close()
            await state.db.close()
            state.password_pool.close()
            lock.release()

    app = FastAPI(title="MCP Manager", lifespan=lifespan)
    app.state.config = config
    app.state.ready = False

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, exc):
        busy = exc.code in {"busy", "queue_timeout"}
        return JSONResponse(
            {"detail": str(exc), "code": exc.code, **exc.details},
            status_code=429 if busy else 502,
            headers={"Retry-After": "1"} if busy else None,
        )

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ZoneInfoNotFoundError)
    async def timezone_error(request: Request, exc):
        return JSONResponse({"detail": "Unknown timezone"}, status_code=422)

    @app.exception_handler(ValidationError)
    async def schema_error(request: Request, exc):
        return JSONResponse({"detail": exc.message}, status_code=422)

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/readyz")
    async def ready():
        return JSONResponse({"ready": app.state.ready}, status_code=200 if app.state.ready else 503)

    for router in (
        identity.router,
        catalog_api.router,
        credentials_api.router,
        operations_api.router,
        audit_api.router,
        leases_api.router,
        storage_api.router,
        oauth_router,
        oauth_metadata.router,
        oauth_public_router,
        embedding_api.router,
        proposals.router,
    ):
        app.include_router(router)
    try:
        from .gateway import install_gateway
        install_gateway(app)
    except ModuleNotFoundError as exc:
        if exc.name != "mcp_manager.gateway":
            raise
    from .cors import GatewayCORSMiddleware
    app.add_middleware(GatewayCORSMiddleware, root_app=app)
    app.add_middleware(AuditContextMiddleware)
    frontend = PACKAGE_ROOT / "static"
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="console")
    return app
