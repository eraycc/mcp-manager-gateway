"""Retention and idle checks use only temporary stores and controlled clocks."""
import asyncio
import importlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_runtime_lifecycle import Connection

from mcp_manager.config import Settings
from mcp_manager.database import Database, set_setting
from mcp_manager.gateway import Gateway
from mcp_manager.logs import LogStore
from mcp_manager.runtime import GatewayError, Runtime, ServerSpec

app_module = importlib.import_module("mcp_manager.app")
CURRENT = datetime(2026, 9, 12, 12, tzinfo=UTC)
EVENTS = [
    ("ancient", "2020-01-01T00:00:00Z"),
    ("old", "2026-09-05T11:59:59.999999Z"),
    ("boundary", "2026-09-05T12:00:00Z"),
    ("recent", "2026-09-05T12:00:00.000001Z"),
]


@pytest.fixture
async def retention_app(tmp_path, monkeypatch):
    db = Database(Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path}/db.sqlite"))
    await db.initialize()
    logs = LogStore(tmp_path)
    state = SimpleNamespace(db=db, logs=logs, runtime=Runtime(None), retention_next=0, refresh_next=None)
    monkeypatch.setattr(app_module, "now", lambda: CURRENT)
    try:
        yield SimpleNamespace(state=state)
    finally:
        await state.runtime.close()
        await logs.close()
        await db.close()


async def maintenance_pass(app, monkeypatch):
    async def stop_after_pass(seconds):
        raise asyncio.CancelledError
    # Replace only this module's clock/sleep surface, leaving DB/transport awaits real.
    monkeypatch.setattr(app_module, "asyncio", SimpleNamespace(
        CancelledError=asyncio.CancelledError,
        get_running_loop=asyncio.get_running_loop,
        sleep=stop_after_pass,
    ))
    with pytest.raises(asyncio.CancelledError):
        await app_module.maintenance(app)


@pytest.mark.parametrize(("days", "expected"), [
    (0, {"ancient", "old", "boundary", "recent"}),
    (7, {"boundary", "recent"}),
])
async def test_retention_removes_only_older_events_from_jsonl_and_rebuilt_index(
        retention_app, monkeypatch, days, expected):
    state = retention_app.state
    await set_setting(state.db, "log_retention_days", days)
    for event_id, stamp in EVENTS:
        await state.logs.append({"id": event_id, "timestamp": stamp, "status": "success"})
    await maintenance_pass(retention_app, monkeypatch)
    assert {row["id"] for row in (await state.logs.query())["items"]} == expected
    persisted = {
        json.loads(line)["id"]
        for path in state.logs.root.glob("????-??-??.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    }
    assert persisted == expected
    await state.logs.rebuild()
    assert {row["id"] for row in (await state.logs.query())["items"]} == expected
    assert not list((state.logs.root / "audit").glob("*.jsonl"))


async def test_retention_change_during_cleanup_is_applied_on_next_pass(retention_app, monkeypatch):
    state = retention_app.state
    await set_setting(state.db, "log_retention_days", 30)
    await state.logs.append({"id": "eight-days", "timestamp": "2026-09-04T12:00:00Z", "status": "success"})
    delete = state.logs.delete

    async def update_during_delete(*args, **kwargs):
        result = await delete(*args, **kwargs)
        await set_setting(state.db, "log_retention_days", 7)
        state.retention_next = 0  # Settings API signals that the policy changed.
        return result

    monkeypatch.setattr(state.logs, "delete", update_during_delete)
    await maintenance_pass(retention_app, monkeypatch)
    assert (await state.logs.query())["total"] == 1
    monkeypatch.setattr(state.logs, "delete", delete)
    await maintenance_pass(retention_app, monkeypatch)
    assert (await state.logs.query())["total"] == 0


@pytest.mark.parametrize("cleanup", ["release", "bridge_ttl"])
async def test_idle_zero_keeps_business_instance_but_allows_lifecycle_cleanup(cleanup):
    clock = [0.0]
    connection = Connection()
    runtime = Runtime(connection.connect, clock=lambda: clock[0], idle_seconds=0)
    lease = runtime.create_lease("u", "t", ttl=90 if cleanup == "bridge_ttl" else None,
                                 kind="bridge" if cleanup == "bridge_ttl" else "agent")
    try:
        await runtime.call(ServerSpec("s", "stdio", {}), lease.id, "call", {})
        clock[0] = 89 if cleanup == "bridge_ttl" else 10**9
        await runtime.reap()
        assert len(runtime.status()) == 1
        assert runtime.status()[0]["phase"] == "ready"
        assert connection.closed == 0
        if cleanup == "release":
            await runtime.release(lease.id)
        else:
            clock[0] = 90
            await runtime.reap()
            with pytest.raises(GatewayError, match="Lease expired"):
                runtime.heartbeat(lease.id, "u", "t")
        assert runtime.instances == {}
        assert lease.id not in runtime.leases
        assert connection.closed == 1
    finally:
        await runtime.close()


async def test_idle_zero_keeps_gateway_session_until_explicit_close():
    clock = [0.0]
    connection = Connection()
    runtime = Runtime(connection.connect, clock=lambda: clock[0], idle_seconds=0)
    gateway = Gateway(SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    lease = runtime.create_lease("anonymous", "client", kind="retention")
    gateway.sessions["sid"] = {"owner": ("anonymous", "client"), "lease": lease.id, "touched": -10**9}
    try:
        await runtime.call(ServerSpec("s", "stdio", {}), lease.id, "call", {})
        clock[0] = 10**9
        await runtime.reap()
        await gateway.reap()
        assert "sid" in gateway.sessions
        assert len(runtime.instances) == 1
        await gateway.close_session("sid")
        assert gateway.sessions == {}
        assert runtime.instances == {}
        assert connection.closed == 1
    finally:
        await runtime.close()
