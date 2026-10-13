"""Downstream failures must not leak configured secrets in HTTP, jobs or cache."""
from test_proposal_atomicity import propose

from mcp_manager.runtime import GatewayError

MARKER = "SYNTHETIC_SECRET_MARKER_NOT_A_REAL_CREDENTIAL"


async def test_proposal_failure_is_redacted_in_response_and_database(db_app, monkeypatch):
    identity = await propose(db_app, config={
        "command": "synthetic-never-launched", "env": {"PRIVATE_KEY": MARKER}
    })
    async def failed(spec, lease):
        raise GatewayError("connection_error", "synthetic failure includes " + spec.config["env"]["PRIVATE_KEY"])
    monkeypatch.setattr(db_app.app.state.runtime, "discover", failed)
    response = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test")
    assert response.status_code == 502
    assert MARKER not in response.text
    detail = (await db_app.web.get(f"/api/v1/mcp-proposals/{identity}")).json()
    assert MARKER not in detail["test_error"] and detail["test_status"] == "failed"


async def test_batch_failure_is_redacted_in_job_result(db_app, monkeypatch):
    import asyncio
    identity = await propose(db_app, config={
        "command": "synthetic-never-launched", "env": {"PRIVATE_KEY": MARKER}
    })
    async def failed(spec, lease):
        raise GatewayError("connection_error", "Synthetic failure " + MARKER)
    monkeypatch.setattr(db_app.app.state.runtime, "discover", failed)
    response = await db_app.web.post("/api/v1/mcp-proposals/batch-test", json={"ids": [identity]})
    job = response.json()
    await asyncio.wait_for(db_app.app.state.jobs.tasks[job["id"]], 5)
    result = await db_app.web.get("/api/v1/jobs/" + job["id"])
    assert MARKER not in result.text


async def test_refresh_failure_is_redacted_in_cache_and_audit(db_app, monkeypatch):
    import json

    from mcp_manager.database import McpServer
    catalog = db_app.app.state.catalog
    row = await catalog.create({
        "name": "Sensitive startup", "mode": "disabled", "transport": "stdio",
        "config": {"command": "synthetic-never-launched", "env": {"PRIVATE_KEY": MARKER}}
    })
    async with db_app.db.locked() as session:
        (await session.get(McpServer, row.id)).mode = "lazy"
    async def failed(spec, *args, **kwargs):
        raise GatewayError("connection_error", "Synthetic failure " + spec.config["env"]["PRIVATE_KEY"])
    monkeypatch.setattr(db_app.app.state.runtime, "perform", failed)
    try:
        await catalog.warm(row.id)
    except GatewayError as error:
        assert MARKER not in str(error)
    latest = await catalog.get(row.id)
    assert MARKER not in json.dumps(catalog.cached(latest))
    logs = await db_app.web.get("/api/v1/logs")
    assert logs.status_code == 200
    assert MARKER not in logs.text
    for item in logs.json()["items"]:
        event = await db_app.app.state.logs.detail(item["id"])
        assert MARKER not in json.dumps(event)


async def test_url_query_and_nested_exception_are_redacted_in_diagnosis(db_app, monkeypatch):
    async def failed(spec, lease):
        try:
            raise ValueError("Synthetic failure " + MARKER)
        except ValueError as cause:
            raise GatewayError("connection_error", "wrapper") from cause
    monkeypatch.setattr(db_app.app.state.runtime, "discover", failed)
    response = await db_app.web.post("/api/v1/mcps/diagnose", json={
        "transport": "streamable-http",
        "config": {"url": "https://example.invalid/mcp?api_key=" + MARKER}
    })
    assert response.status_code == 502
    assert MARKER not in response.text


def test_failure_status_details_survive_redaction():
    import json

    from mcp_manager.diagnostics import safe_gateway_error

    error = GatewayError("startup_failed", "Synthetic " + MARKER, {
        "startup_failure_count": 2, "status": "stopped", "last_startup_error": MARKER
    })
    result = safe_gateway_error(error, config={"env": {"PRIVATE_KEY": MARKER}})
    assert result.details.get("startup_failure_count") == 2
    assert result.details.get("status") == "stopped"
    assert MARKER not in json.dumps(result.details)


async def test_runtime_status_never_exposes_unredacted_startup_error():
    import asyncio
    import json
    from contextlib import asynccontextmanager

    from mcp_manager.runtime import Runtime, ServerSpec

    entered, release = asyncio.Event(), asyncio.Event()
    @asynccontextmanager
    async def connector(spec):
        raise GatewayError("connection_error", "Synthetic " + MARKER)
        yield None
    async def record(spec, exc):
        entered.set()
        await release.wait()
    runtime = Runtime(connector)
    runtime.on_start_failure = record
    lease = runtime.create_lease("synthetic-user", "synthetic-token")
    spec = ServerSpec("synthetic", "stdio", {"command": "unused", "env": {"PRIVATE_KEY": MARKER}})
    task = asyncio.create_task(runtime.discover(spec, lease.id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert MARKER not in json.dumps(runtime.status())
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
