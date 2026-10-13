"""Isolated, real database targets for the release matrix; never use business schemas."""
import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from mcp_manager.database import Database


def validate_test_url(value):
    url = make_url(value)
    if url.get_backend_name() != "mysql" or not (url.database or "").startswith("mmg_test_"):
        raise ValueError("MySQL test URLs must target an isolated mmg_test_ schema")
    return url.set(drivername="mysql+asyncmy")


def docker(*args, timeout=120):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        # Do not include command/environment/URL credentials in errors.
        raise RuntimeError(f"Docker operation {args[0]} failed")
    return result.stdout.strip()


@asynccontextmanager
async def managed_mysql_database(version):
    if version not in ("mysql80", "mysql84"):
        raise ValueError("Unsupported MySQL test version")
    name = f"mmg-test-{version}-{uuid4().hex}"
    image = "mysql:8.0" if version == "mysql80" else "mysql:8.4"
    started = False
    try:
        await asyncio.to_thread(
            docker, "run", "--detach", "--name", name, "--label", "mmg.test=isolated",
            "--publish", "127.0.0.1::3306", "--env", "MYSQL_ROOT_PASSWORD=mmg-test-only",
            "--env", "MYSQL_ROOT_HOST=%", image, timeout=600,
        )
        started = True
        port_line = await asyncio.to_thread(docker, "port", name, "3306/tcp")
        port = int(port_line.splitlines()[0].rsplit(":", 1)[1])
        url = make_url(f"mysql+asyncmy://root:mmg-test-only@127.0.0.1:{port}/mysql")
        engine = create_async_engine(url, connect_args={"connect_timeout": 2}, pool_pre_ping=True)
        try:
            deadline = asyncio.get_running_loop().time() + 180
            while True:
                try:
                    async with engine.connect() as connection:
                        await connection.execute(text("SELECT 1"))
                    break
                except (SQLAlchemyError, OSError):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError("Isolated MySQL container did not become ready") from None
                    await asyncio.sleep(0.5)
            yield url
        finally:
            await engine.dispose()
    finally:
        if started:
            # Exact unique name created above; no volumes or unrelated containers.
            await asyncio.to_thread(docker, "rm", "--force", name)


@asynccontextmanager
async def isolated_mysql_schema(admin_url):
    url = make_url(admin_url)
    name = f"mmg_test_{uuid4().hex}"
    engine = create_async_engine(url.set(database="mysql"), pool_pre_ping=True)
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"))
        created = True
        yield url.set(database=name).render_as_string(hide_password=False)
    finally:
        if created:
            async with engine.begin() as connection:
                await connection.execute(text(f"DROP DATABASE `{name}`"))
        await engine.dispose()


@asynccontextmanager
async def database_target(backend, managed=False):
    if backend == "sqlite":
        yield None
        return
    env_name = "MCP_TEST_MYSQL80_URL" if backend == "mysql80" else "MCP_TEST_MYSQL84_URL"
    value = os.environ.get(env_name)
    if value:
        admin = validate_test_url(value)
        async with isolated_mysql_schema(admin) as url:
            yield url
    elif managed:
        async with managed_mysql_database(backend) as admin, isolated_mysql_schema(admin) as url:
            yield url
    else:
        raise RuntimeError(f"{env_name} is missing; provide an isolated URL or --managed-mysql")


@dataclass
class DbHarness:
    db: Database
    app: object
    web: httpx.AsyncClient
    actor: object
