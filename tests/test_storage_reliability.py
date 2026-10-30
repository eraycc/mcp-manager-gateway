"""Regressions for interrupted batches, lock recovery and MCP health boundaries."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from filelock import FileLock

from mcp_manager.storage_gate import StorageGate


def test_failed_maintenance_probe_releases_all_locks(tmp_path, monkeypatch):
    gate = StorageGate(tmp_path)
    original = type(gate.workers).glob

    def failed_scan(path, pattern):
        if path == gate.workers:
            raise OSError("fixture worker scan failed")
        return original(path, pattern)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(type(gate.workers), "glob", failed_scan)
            with pytest.raises(OSError, match="worker scan"), gate.maintenance():
                pass
        assert not gate.admission.is_locked
        with FileLock(gate.admission.lock_file, timeout=0):
            pass
        with gate.operation():
            pass
    finally:
        gate.admission.release(force=True)
        gate.close()


def test_busy_maintenance_does_not_hold_global_admission(tmp_path):
    first, second, third = (StorageGate(tmp_path) for _ in range(3))
    try:
        with first.operation(), second.maintenance() as admitted:
            assert not admitted
            with FileLock(second.admission.lock_file, timeout=0):
                pass
            with third.operation():
                pass
    finally:
        for gate in (first, second, third):
            gate.close()


def test_concurrent_operations_leave_no_orphaned_native_lock(tmp_path):
    gate = StorageGate(tmp_path)

    def perform(_):
        for _ in range(40):
            with gate.operation():
                pass

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(perform, range(32)))
        assert gate.count == 0
        with FileLock(gate.admission.lock_file, timeout=0):
            pass
        with FileLock(str(gate.activity_path), timeout=0):
            pass
    finally:
        gate.close()


async def test_repeated_cancelled_operations_release_activity(tmp_path):
    gate, peer = StorageGate(tmp_path), StorageGate(tmp_path)
    entered = asyncio.Event()

    async def request():
        async with gate.async_operation():
            entered.set()
            await asyncio.Event().wait()

    try:
        for _ in range(10):
            entered.clear()
            task = asyncio.create_task(request())
            await entered.wait()
            task.cancel()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert gate.count == 0
            with peer.maintenance() as admitted:
                assert admitted
    finally:
        gate.close()
        peer.close()


async def test_business_error_does_not_touch_catalog_health(db_app):
    from test_consecutive_operation_failures import call, healthy_service

    row, faults, context = await healthy_service(db_app, "http")
    catalog = db_app.app.state.catalog
    before = catalog.cached(row, db_app.actor["id"])
    faults["call"] = True
    for _ in range(4):
        await call(db_app, row, context, fail=True)
    after = catalog.cached(row, db_app.actor["id"])
    for key in ("startup_failure_count", "cache_at", "cache_error", "cache_stale",
                "last_startup_error", "last_refresh_error", "runtime_status"):
        assert after.get(key) == before.get(key), key
    assert after["tool_count"] == before["tool_count"] == 1
    assert faults["calls"] == 4


async def test_response_validation_failure_is_not_mcp_unavailability_or_replayed(db_app):
    from test_consecutive_operation_failures import call, healthy_service

    row, faults, context = await healthy_service(db_app, "http")
    catalog = db_app.app.state.catalog
    before = catalog.cached(row, db_app.actor["id"])
    faults["validation"] = True
    for _ in range(4):
        await call(db_app, row, context, fail=True)
    after = catalog.cached(row, db_app.actor["id"])
    assert faults["calls"] == 4
    assert after["startup_failure_count"] == 0 and after["cache_at"] == before["cache_at"]
    assert not after["cache_error"] and not after["last_startup_error"]
    faults["validation"] = False
    await call(db_app, row, context)
    assert faults["calls"] == 5


async def test_audit_diagnostics_distinguish_nine_historical_failures_from_recovery(tmp_path, monkeypatch):
    import errno

    from mcp_manager.logs import LogStore

    logs = LogStore(tmp_path)
    original = logs.audits._write
    try:
        def full(event):
            raise OSError(errno.ENOSPC, "fixture secret must not appear in diagnostics")
        monkeypatch.setattr(logs.audits, "_write", full)
        for _ in range(9):
            assert await logs.audit("mcp.update", "u", {"mcp_id": "x"}) is None
        status = await logs.audits.status()
        assert status["write_failures"] == status["consecutive_write_failures"] == 9
        assert status["last_error"]["reason"] == "disk_full"
        assert status["last_error"]["phase"] == "jsonl_write"
        assert "secret" not in str(status)
        monkeypatch.setattr(logs.audits, "_write", original)
        identity = await logs.audit("mcp.update", "u", {"mcp_id": "x"})
        assert identity and await logs.audits.detail(identity)
        status = await logs.audits.status()
        assert status["write_failures"] == 9 and status["consecutive_write_failures"] == 0
        assert status["last_write_success_at"] and status["index_failures"] == 0
    finally:
        await logs.close()


async def test_audit_source_lock_timeout_has_safe_diagnostics_and_recovers(tmp_path, monkeypatch):
    from filelock import Timeout

    from mcp_manager.logs import LogStore

    logs = LogStore(tmp_path)
    try:
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise Timeout("fixture")
            patch.setattr(logs.audits.source_lock, "acquire", fail)
            assert await logs.audit("mcp.update", "u", {}) is None
        status = await logs.audits.status()
        assert status["last_error"]["reason"] == "lock_timeout"
        assert status["last_error"]["phase"] == "source_lock"
        assert await logs.audit("mcp.update", "u", {})
        assert (await logs.audits.status())["consecutive_write_failures"] == 0
    finally:
        await logs.close()


async def test_batch_mode_changes_continue_without_observer_and_leave_console_usable(db_app, monkeypatch):
    from test_consecutive_operation_failures import healthy_service

    first, _, _ = await healthy_service(db_app, "http")
    state = db_app.app.state
    rows = [first]
    for number in range(5):
        rows.append(await state.catalog.create({
            "name": f"Batch fixture {number}", "transport": "streamable-http",
            "config": {"url": "https://mcp.test/mcp"},
        }, user_id=db_app.actor["id"], discover=False))
    original = state.catalog.update
    entered, resume = asyncio.Event(), asyncio.Event()

    async def paused(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(state.catalog, "update", paused)
    try:
        for action, mode in (("enable", "eager"), ("lazy", "lazy")):
            entered.clear()
            resume.clear()
            response = await db_app.web.post("/api/v1/mcps/batch", json={"ids": [r.id for r in rows], "action": action})
            assert response.status_code == 200, response.text
            job = response.json()
            await asyncio.wait_for(entered.wait(), 10)
            # A closed observer never cancels the independently submitted job.
            future = state.jobs.tasks[job["id"]]
            assert not future.done()
            resume.set()
            await asyncio.wait_for(asyncio.shield(future), 60)
            status = await state.jobs.store.get_status(job["id"])
            assert status["status"] == "completed", status
            assert status["succeeded"] == len(rows) and status["failed"] == 0
            for row in rows:
                assert (await state.catalog.get(row.id)).mode == mode
        for path in ("/api/v1/dashboard", "/api/v1/tokens", "/api/v1/mcps", "/api/v1/audit-logs/status"):
            response = await db_app.web.get(path)
            assert response.status_code == 200, (path, response.text)
        assert (await db_app.web.post(f"/api/v1/mcps/{first.id}/refresh")).status_code == 200
        assert (await state.logs.audits.status())["write_failures"] == 0
    finally:
        resume.set()


@pytest.mark.parametrize("library", ["httpx", "httpx2"])
def test_network_evidence_in_both_http_clients_is_an_availability_failure(library):
    import importlib

    from mcp_manager.protocol_errors import is_connection_failure

    http = importlib.import_module(library)
    assert is_connection_failure(http.ConnectError("fixture"))
    assert is_connection_failure(http.ReadError("fixture"))
    assert not is_connection_failure(http.ReadTimeout("fixture"))
    assert not is_connection_failure(ValueError("invalid tool response"))
