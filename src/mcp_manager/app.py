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

from . import catalog_api, embedding_api, identity, operations_api
from .embeddings import EmbeddingIndex
from .catalog import Catalog
from .config import PACKAGE_ROOT, Settings
from .database import ApiToken, Database, User, get_setting, now
from .jobs import Jobs
from .logs import LogStore
from .oauth import OAuth, router as oauth_router
from .runtime import GatewayError, Runtime
from .transports import connect, load_transport_plugins


async def maintenance(app):
    state = app.state
    while True:
        try:
            await state.runtime.reap()
            if hasattr(state, "gateway"):
                await state.gateway.reap()
            # Revocation also releases existing downstream leases promptly.
            async with state.db.session() as s:
                for lease in list(state.runtime.leases.values()):
                    if lease.token_id in {"maintenance", "test", "diagnostic"}:
                        continue
                    if lease.user_id and lease.user_id != "anonymous":
                        user = await s.get(User, lease.user_id)
                        token = await s.get(ApiToken, lease.token_id) if lease.token_id else None
                        if (not user or user.disabled or not token or token.disabled
                                or identity.expired(token.expires_at)):
                            await state.runtime.release(lease.id)
                        else:
                            allowed = await identity.effective_mcp_ids(state.db, user, token)
                            await state.runtime.restrict_lease(lease.id, allowed)
                # Eager personal instances can have no leases after warm-up.
                for instance in list(state.runtime.instances.values()):
                    owner = (instance.spec.credential_owner if instance.spec.credential_owner not in {"", "service"}
                             else instance.key[-1] if instance.spec.isolation == "user" else None)
                    if owner:
                        user = await s.get(User, owner)
                        if (not user or user.disabled or (user.role != "admin" and user.scope_mode != "all"
                                                         and instance.spec.id not in user.mcp_ids)):
                            instance.refs.clear()
                            await state.runtime._stop_instance(instance)
                            if instance.spec.credential_owner:
                                state.runtime.credential_versions[(instance.spec.id, owner)] = None
            anonymous_allowed = set()
            if not await get_setting(state.db, "token_auth_enabled", True):
                anonymous_allowed = {row.id for row in await state.catalog.rows() if row.mode != "disabled"}
                if await get_setting(state.db, "anonymous_scope_mode", "selected") != "all":
                    anonymous_allowed &= set(await get_setting(state.db, "anonymous_mcp_ids", []))
            for lease in list(state.runtime.leases.values()):
                if lease.user_id == "anonymous":
                    await state.runtime.restrict_lease(lease.id, anonymous_allowed)
            if await get_setting(state.db, "refresh_enabled", False):
                trigger = CronTrigger.from_crontab(await get_setting(state.db, "refresh_cron", "0 3 * * *"),
                                                   timezone=await get_setting(state.db, "timezone", "Asia/Shanghai"))
                current = now()
                if state.refresh_next is None:
                    state.refresh_next = trigger.get_next_fire_time(None, current)
                elif current >= state.refresh_next:
                    targets = await state.catalog.refresh_targets()
                    state.jobs.submit("mcp.scheduled_refresh", targets, state.catalog.refresh_target)
                    state.refresh_next = trigger.get_next_fire_time(state.refresh_next, current)
            else:
                state.refresh_next = None
            if asyncio.get_running_loop().time() >= state.retention_next:
                # Reserve before yielding so a concurrent settings update can
                # request another pass without its reset being overwritten.
                state.retention_next = asyncio.get_running_loop().time() + 3600
                try:
                    days = await get_setting(state.db, "log_retention_days", 0)
                    if days:
                        from datetime import timedelta
                        # Log filters are inclusive and timestamps use microseconds.
                        # Keep records exactly at the retention boundary.
                        cutoff = now() - timedelta(days=days, microseconds=1)
                        await state.logs.delete(to_time=cutoff.isoformat())
                except Exception:
                    state.retention_next = 0
                    raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await state.logs.audit("maintenance.error", None, {"error": str(exc)})
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
        load_transport_plugins()
        state.runtime = Runtime(connect)
        state.jobs = Jobs(config.data_dir)
        state.catalog = Catalog(state.db, state.runtime, state.logs, config)
        state.embeddings = EmbeddingIndex(config.data_dir)
        state.oauth = OAuth(state.catalog)
        state.catalog.oauth = state.oauth
        state.runtime.on_starting = state.catalog.connection_starting
        state.runtime.on_connect = state.catalog.connection_ready
        state.runtime.on_start_failure = state.catalog.start_failed
        state.refresh_next = None
        state.retention_next = 0
        task = None
        try:
            await state.db.initialize()
            state.cors_origins = await get_setting(state.db, "cors_origins", ["*"])
            from zoneinfo import ZoneInfo
            state.logs.timezone = ZoneInfo(await get_setting(state.db, "timezone", "Asia/Shanghai"))
            state.runtime.idle_seconds = await get_setting(state.db, "idle_seconds", 86400)
            async with contextlib.AsyncExitStack() as stack:
                if hasattr(state, "protocol"):
                    await stack.enter_async_context(state.protocol.session_manager.run())
                eager = {row.id for row in await state.catalog.rows()
                         if row.mode == "eager" and row.isolation != "session"
                         and (row.isolation == "service" or
                              state.catalog.unseal(row.config).get("auth", {}).get("scope") == "user")}
                rows = {row.id: row for row in await state.catalog.rows()}
                targets = [target for target in await state.catalog.refresh_targets()
                           if target["server_id"] in eager or state.catalog.cached(
                               rows[target["server_id"]], target["user_id"])["cache_status"] == "empty"]
                if targets:
                    state.jobs.submit("mcp.startup", targets, state.catalog.warm_target)
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
            lock.release()

    app = FastAPI(title="MCP Manager", lifespan=lifespan)
    app.state.config = config
    app.state.ready = False

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, exc):
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=502)

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

    for router in (identity.router, catalog_api.router, operations_api.router, oauth_router, embedding_api.router):
        app.include_router(router)
    try:
        from .gateway import install_gateway
        install_gateway(app)
    except ModuleNotFoundError as exc:
        if exc.name != "mcp_manager.gateway":
            raise
    from .cors import GatewayCORSMiddleware
    app.add_middleware(GatewayCORSMiddleware, root_app=app)
    frontend = PACKAGE_ROOT / "static"
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="console")
    return app
