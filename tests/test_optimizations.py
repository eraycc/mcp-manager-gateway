import json
from types import SimpleNamespace

import pytest

import mcp_manager.app as app_module
from mcp_manager.catalog import SECRET_KEYS
from mcp_manager.config import Settings
from mcp_manager.cors import GatewayCORSMiddleware
from mcp_manager.database import Database, SystemSetting, get_setting, set_setting
from mcp_manager.logs import LogStore, redact
from mcp_manager.operations_api import (
    DashboardEventBuffer,
    format_sse_heartbeat,
)
from mcp_manager.runtime import Instance, Runtime, ServerSpec


def test_secret_redaction_uses_one_shared_superset():
    import mcp_manager.logs as logs_module

    assert logs_module.SECRET_KEYS is SECRET_KEYS
    assert {
        "current_password", "secret", "secret_key", "database_url",
        "password", "token", "client_secret", "api_key",
    } <= SECRET_KEYS
    assert redact({
        "current_password": "one",
        "database_url": "two",
        "headers": {"X-Key": "three"},
    }) == {
        "current_password": "[REDACTED]",
        "database_url": "[REDACTED]",
        "headers": {"X-Key": "[REDACTED]"},
    }


@pytest.mark.asyncio
async def test_maintenance_steps_continue_after_one_failure(monkeypatch):
    calls = []
    audits = []

    async def failed(_app):
        calls.append("failed")
        raise RuntimeError("first failed")

    async def succeeded(_app):
        calls.append("succeeded")

    monkeypatch.setattr(app_module, "MAINTENANCE_STEPS", (failed, succeeded))
    state = SimpleNamespace(
        logs=SimpleNamespace(
            audit=lambda action, user_id, details: _record_audit(
                audits, action, user_id, details
            )
        )
    )
    await app_module._maintenance_cycle(SimpleNamespace(state=state))
    assert calls == ["failed", "succeeded"]
    assert audits == [
        ("maintenance.error", None, {"step": "failed", "error": "first failed"})
    ]


async def _record_audit(target, *values):
    target.append(values)


@pytest.mark.asyncio
async def test_runtime_exposes_public_lifecycle_boundary(monkeypatch):
    runtime = Runtime(None)
    spec = ServerSpec("service", "stdio", {"command": "unused"})
    instance = Instance(("service", 1, "", "service"), spec)
    stopped = []

    async def stop(target, **options):
        stopped.append((target, options))

    available = []
    monkeypatch.setattr(runtime, "_stop_instance", stop)
    monkeypatch.setattr(runtime, "_available", available.append)
    await runtime.stop_instance(instance, force=True)
    runtime.mark_available(spec)
    assert stopped == [(instance, {"force": True, "only_unreferenced": False})]
    assert available == [spec]


def test_cors_validated_host_cache_invalidates_on_settings_change(monkeypatch):
    import mcp_manager.cors as cors_module

    calls = []
    original = cors_module.validate_hosts

    def counted(values):
        calls.append(tuple(values))
        return original(values)

    monkeypatch.setattr(cors_module, "validate_hosts", counted)
    root = SimpleNamespace(state=SimpleNamespace(allowed_hosts=["localhost"]))
    middleware = GatewayCORSMiddleware(None, root)
    first = middleware.validated_hosts()
    second = middleware.validated_hosts()
    assert first is second
    assert calls == [("localhost",)]

    root.state.allowed_hosts = ["127.0.0.1"]
    assert middleware.validated_hosts() != first
    assert calls == [("localhost",), ("127.0.0.1",)]


@pytest.mark.asyncio
async def test_cors_invalid_runtime_settings_fail_closed():
    async def downstream(_scope, _receive, _send):
        raise AssertionError("invalid Host policy must not reach the application")

    root = SimpleNamespace(state=SimpleNamespace(
        allowed_hosts=["https://invalid/path"],
        cors_origins=["*"],
        config=SimpleNamespace(public_url="http://localhost"),
    ))
    middleware = GatewayCORSMiddleware(downstream, root)
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await middleware({
        "type": "http",
        "method": "GET",
        "path": "/mcp",
        "headers": [(b"host", b"localhost")],
    }, receive, send)
    assert messages[0]["status"] == 403
    body = b"".join(message.get("body", b"") for message in messages)
    assert b"Host is not allowed" in body
    assert b"allowed_hosts" in body


@pytest.mark.asyncio
async def test_setting_cache_has_ttl_and_set_invalidates(tmp_path):
    db = Database(Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path}/settings-cache.db",
    ))
    await db.initialize()
    try:
        await set_setting(db, "cached", "one")
        assert await get_setting(db, "cached") == "one"
        async with db.locked() as session:
            row = await session.get(SystemSetting, "cached")
            row.value = "two"
        assert await get_setting(db, "cached") == "one"

        await set_setting(db, "cached", "three")
        assert await get_setting(db, "cached") == "three"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_log_startup_only_indexes_incremental_bytes(tmp_path, monkeypatch):
    first = LogStore(tmp_path)
    await first.append({
        "id": "old", "timestamp": "2026-09-22T00:00:00+00:00",
        "status": "success",
    })
    await first.close()

    indexed = []
    original = LogStore._index

    def tracked(self, event, path, offset, size):
        indexed.append(event["id"])
        return original(self, event, path, offset, size)

    monkeypatch.setattr(LogStore, "_index", tracked)
    unchanged = LogStore(tmp_path)
    assert indexed == []
    await unchanged.close()

    path = tmp_path / "logs" / "2026-09-22.jsonl"
    event = {
        "id": "new", "timestamp": "2026-09-22T00:00:01+00:00",
        "status": "success", "source": "gateway", "duration_ms": 0,
    }
    with path.open("ab") as stream:
        stream.write((json.dumps(event) + "\n").encode())
    incremental = LogStore(tmp_path)
    try:
        assert indexed == ["new"]
        assert (await incremental.query())["total"] == 2
    finally:
        await incremental.close()


def test_sse_buffer_replays_ids_and_formats_heartbeat():
    events = DashboardEventBuffer(maxlen=3)
    first = events.publish({"calls": 1})
    second = events.publish({"calls": 2})
    assert first.startswith("id: 1\nevent: dashboard\n")
    assert second.startswith("id: 2\nevent: dashboard\n")
    assert events.replay("1") == [second]
    assert events.replay("not-a-number") == [first, second]
    assert format_sse_heartbeat() == ": heartbeat\n\n"


