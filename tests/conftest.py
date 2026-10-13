"""Tests never inherit a real user's database or secret configuration."""
import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def isolated_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_MANAGER_HOME", str(tmp_path / "config-home"))
    for name in ("DATABASE_URL", "DATA_DIR", "SECRET_KEY", "HOST", "PORT", "PUBLIC_URL", "COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolated_sse_shutdown(monkeypatch):
    # sse-starlette copies Uvicorn shutdown into a process-wide flag.
    # Each test starts its own server; a previous server's exit must not
    # immediately cancel the next test's SSE response.
    from sse_starlette.sse import AppStatus

    monkeypatch.setattr(AppStatus, "should_exit", False)


def select_backends(values, required=False):
    requested = list(dict.fromkeys(values or (["sqlite", "mysql80", "mysql84"] if required else ["sqlite"])))
    if required and set(requested) != {"sqlite", "mysql80", "mysql84"}:
        raise ValueError("--require-mysql requires sqlite, mysql80 and mysql84")
    return requested


def pytest_addoption(parser):
    group = parser.getgroup("database matrix")
    group.addoption("--db-backend", action="append", choices=["sqlite", "mysql80", "mysql84"], default=[])
    group.addoption("--require-mysql", action="store_true", default=False)
    group.addoption("--managed-mysql", action="store_true", default=False)


def pytest_generate_tests(metafunc):
    if "db_backend" in metafunc.fixturenames:
        try:
            backends = select_backends(
                metafunc.config.getoption("--db-backend"), metafunc.config.getoption("--require-mysql")
            )
        except ValueError as exc:
            raise pytest.UsageError(str(exc)) from None
        metafunc.parametrize("db_backend", backends)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def managed_mysql_targets(pytestconfig):
    import os
    from contextlib import AsyncExitStack

    from db_fixture import managed_mysql_database

    targets = {}
    async with AsyncExitStack() as stack:
        if pytestconfig.getoption("--managed-mysql"):
            for backend in select_backends(
                pytestconfig.getoption("--db-backend"), pytestconfig.getoption("--require-mysql")
            ):
                env = "MCP_TEST_MYSQL80_URL" if backend == "mysql80" else "MCP_TEST_MYSQL84_URL"
                if backend != "sqlite" and not os.environ.get(env):
                    targets[backend] = await stack.enter_async_context(managed_mysql_database(backend))
        yield targets


@pytest.fixture
async def db_settings(db_backend, tmp_path, pytestconfig, managed_mysql_targets):
    from db_fixture import database_target, isolated_mysql_schema

    from mcp_manager.config import Settings

    if db_backend in managed_mysql_targets:
        async with isolated_mysql_schema(managed_mysql_targets[db_backend]) as url:
            yield Settings(data_dir=tmp_path / "matrix", database_url=url, secret_key="isolated-matrix-key")
    else:
        async with database_target(db_backend, False) as url:
            yield Settings(data_dir=tmp_path / "matrix", database_url=url or "", secret_key="isolated-matrix-key")


@pytest.fixture
async def dual_db(db_settings):
    from mcp_manager.database import Database

    db = Database(db_settings)
    try:
        await db.initialize()
        yield db
    finally:
        await db.close()


@pytest.fixture
async def db_app(db_settings):
    import asyncio

    import httpx
    from db_fixture import DbHarness

    from mcp_manager.app import create_app

    app = create_app(db_settings)
    ready, finished = asyncio.Event(), asyncio.Event()

    async def lifespan_owner():
        async with app.router.lifespan_context(app):
            ready.set()
            await finished.wait()

    owner = asyncio.create_task(lifespan_owner())
    waiter = asyncio.create_task(ready.wait())
    try:
        done, _ = await asyncio.wait({owner, waiter}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        if owner in done:
            await owner
            raise RuntimeError("Test application stopped during startup")
        if waiter not in done:
            raise TimeoutError("Test application did not start")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as web:
            response = await web.post("/api/v1/auth/register", json={"username": "admin", "password": "password12345"})
            assert response.status_code == 200
            response = await web.post("/api/v1/auth/login", json={"username": "admin", "password": "password12345"})
            assert response.status_code == 200
            web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
            yield DbHarness(db=app.state.db, app=app, web=web, actor=response.json())
    finally:
        waiter.cancel()
        finished.set()
        await asyncio.wait_for(owner, 30)
