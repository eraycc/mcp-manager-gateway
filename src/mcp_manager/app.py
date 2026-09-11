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

from . import catalog_api, identity, operations_api
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
                days = await get_setting(state.db, "log_retention_days", 0)
                if days:
                    from datetime import timedelta
                    await state.logs.delete(to_time=(now() - timedelta(days=days)).isoformat())
                state.retention_next = asyncio.get_running_loop().time() + 3600
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
        state.oauth = OAuth(state.catalog)
        state.catalog.oauth = state.oauth
        state.refresh_next = None
        state.retention_next = 0
        task = None
        try:
            await state.db.initialize()
            state.runtime.idle_seconds = await get_setting(state.db, "idle_seconds", 86400)
            async with contextlib.AsyncExitStack() as stack:
                if hasattr(state, "protocol"):
                    await stack.enter_async_context(state.protocol.session_manager.run())
                for row in await state.catalog.rows():
                    if row.mode == "eager" and row.isolation == "service":
                        state.jobs.submit("mcp.startup", [row.id], state.catalog.warm)
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

    for router in (identity.router, catalog_api.router, operations_api.router, oauth_router):
        app.include_router(router)
    try:
        from .gateway import install_gateway
        install_gateway(app)
    except ModuleNotFoundError as exc:
        if exc.name != "mcp_manager.gateway":
            raise
    frontend = PACKAGE_ROOT / "static"
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="console")
    return app
