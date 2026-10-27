import json
from datetime import UTC, datetime, timedelta

import pytest

from mcp_manager.logs import LogStore


async def test_audit_legacy_index_filters_and_call_rebuild_isolation(tmp_path):
    root = tmp_path / "logs" / "audit"
    root.mkdir(parents=True)
    old = {"event_id": "legacy", "timestamp": "2026-09-11T00:00:00+00:00",
           "action": "mcp.update", "user_id": "old-user", "details": {"mcp_id": "service"}}
    (root / "2026-09-11.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
    logs = LogStore(tmp_path)
    try:
        page = await logs.audits.query(action="mcp.update", user_id="old-user")
        assert page["total"] == 1 and page["items"][0]["result"] == "unknown"
        assert (await logs.audits.detail("legacy"))["target_id"] == "service"
        await logs.append({"id": "call", "status": "success"})
        await logs.rebuild()
        assert (await logs.audits.query())["total"] == 1
        assert (await logs.query())["total"] == 1
        assert json.loads((root / "2026-09-11.jsonl").read_text()) == old
    finally:
        await logs.close()


async def test_audit_redacts_allowlists_and_bounds_details(tmp_path):
    logs = LogStore(tmp_path)
    try:
        identity = await logs.audit("settings.update", "u", {
            "changes": {"title": {"old": "Before", "new": "After"},
                        "translation_config": {"old": {"api_key": "private"}, "new": {"password": "private"}}},
            "unregistered": "must disappear",
        }, username="alice", source="web", target_type="settings", result="success")
        detail = await logs.audits.detail(identity)
        assert "private" not in json.dumps(detail)
        assert "unregistered" not in detail["details"]
        assert detail["username"] == "alice" and detail["result"] == "success"
        assert detail["details"]["changes"]["title"]["new"] == "After"
        await logs.audit("mcp.update", "u", {"mcp_id": "x", "name": "z" * 100000})
        assert (await logs.audits.status())["events"] == 2
    finally:
        await logs.close()


async def test_audit_delete_preserves_marker_and_new_events_and_rebuild(tmp_path):
    logs = LogStore(tmp_path)
    try:
        old = await logs.audit("mcp.create", "u", {"mcp_id": "old"})
        ceiling = await logs.audits.watermark()
        fresh = await logs.audit("mcp.create", "u", {"mcp_id": "fresh"})
        result = await logs.audits.delete(None, max_seq=ceiling, actor={"user_id": "admin", "source": "web"})
        assert result["deleted"] == 1
        assert await logs.audits.detail(old) is None
        assert await logs.audits.detail(fresh)
        assert (await logs.audits.query(action="audit.delete"))["total"] == 2
        await logs.audits.rebuild()
        assert await logs.audits.detail(old) is None
        assert await logs.audits.detail(fresh)
    finally:
        await logs.close()


async def test_audit_recovery_replays_durable_delete_journal(tmp_path, monkeypatch):
    logs = LogStore(tmp_path)
    identity = await logs.audit("mcp.create", "u", {"mcp_id": "gone"})
    original = logs.audits._rewrite_targets

    def crash(operation_id):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(logs.audits, "_rewrite_targets", crash)
    with pytest.raises(OSError):
        await logs.audits.delete([identity], actor={"user_id": "admin"})
    monkeypatch.setattr(logs.audits, "_rewrite_targets", original)
    await logs.close()
    recovered = LogStore(tmp_path)
    try:
        assert await recovered.audits.detail(identity) is None
        assert (await recovered.audits.query(action="audit.delete"))["total"] >= 1
    finally:
        await recovered.close()


async def test_audit_write_failure_is_observable_and_does_not_fail_business(tmp_path, monkeypatch):
    logs = LogStore(tmp_path)
    try:
        def fail(event):
            raise OSError("disk full")
        monkeypatch.setattr(logs.audits, "_write", fail)
        assert await logs.audit("mcp.delete", "u", {"mcp_id": "x"}) is None
        assert (await logs.audits.status())["write_failures"] == 1
        with pytest.raises(OSError):
            await logs.audits.delete(None, actor={"user_id": "admin"})
    finally:
        await logs.close()


async def test_audit_retention_uses_utc_age_and_zero_is_unlimited(tmp_path):
    logs = LogStore(tmp_path)
    try:
        stamp = datetime.now(UTC)
        old = await logs.audit("mcp.create", None, {"mcp_id": "old"},
                               timestamp=(stamp - timedelta(days=8)).isoformat())
        kept = await logs.audit("mcp.create", None, {"mcp_id": "kept"})
        assert (await logs.audits.retain(0, at=stamp))["deleted"] == 0
        assert await logs.audits.detail(old)
        assert (await logs.audits.retain(7, at=stamp))["deleted"] == 1
        assert await logs.audits.detail(old) is None and await logs.audits.detail(kept)
        assert not (tmp_path / "logs" / "audit" / (stamp - timedelta(days=8)).strftime("%Y-%m-%d.jsonl")).exists()
    finally:
        await logs.close()


async def test_partial_tail_and_malformed_lines_are_reported_without_losing_legacy(tmp_path):
    root = tmp_path / "logs" / "audit"
    root.mkdir(parents=True)
    path = root / "2026-10-27.jsonl"
    valid = {"event_id": "first", "timestamp": "2026-10-27T01:00:00Z", "action": "mcp.create",
             "user_id": None, "details": {"mcp_id": "one"}}
    tail = {"event_id": "tail", "timestamp": "2026-10-27T01:00:01Z", "action": "mcp.create",
            "user_id": None, "details": {"mcp_id": "two"}}
    path.write_text(json.dumps(valid) + "\nnot-json\n" + json.dumps(tail), encoding="utf-8")
    logs = LogStore(tmp_path)
    try:
        assert (await logs.audits.query())["total"] == 1
        assert (await logs.audits.status())["skipped_records"] == 1
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        assert (await logs.audits.query())["total"] == 2
        assert (await logs.audits.status())["skipped_records"] == 1
    finally:
        await logs.close()


async def test_utc_retention_preserves_exact_boundary_and_unrelated_calls(tmp_path):
    logs = LogStore(tmp_path)
    try:
        at = datetime(2026, 10, 27, 16, tzinfo=UTC)
        boundary = at - timedelta(days=7)
        expired = await logs.audit("mcp.create", None, {}, timestamp=(boundary - timedelta(microseconds=1)).isoformat())
        kept = await logs.audit("mcp.create", None, {}, timestamp=boundary.isoformat())
        await logs.append({"id": "unrelated", "status": "success"})
        await logs.audits.retain(7, at=at)
        assert await logs.audits.detail(expired) is None
        assert await logs.audits.detail(kept)
        assert (await logs.query())["total"] == 1
    finally:
        await logs.close()


async def test_export_watermark_survives_rebuild_and_excludes_new_events(tmp_path):
    logs = LogStore(tmp_path)
    try:
        ids = {await logs.audit("mcp.create", None, {"mcp_id": str(i)}) for i in range(3)}
        ceiling = await logs.audits.watermark()
        rows, cursor = await logs.audits.export_batch(max_seq=ceiling, limit=1)
        await logs.audit("mcp.create", None, {"mcp_id": "new"})
        await logs.audits.rebuild()
        more, _ = await logs.audits.export_batch(max_seq=ceiling, cursor=cursor)
        assert {event["event_id"] for event in rows + more} == ids
    finally:
        await logs.close()


async def test_projection_failure_keeps_durable_event_for_incremental_recovery(tmp_path, monkeypatch):
    logs = LogStore(tmp_path)
    original = logs.audits._index
    try:
        def fail(*args):
            raise RuntimeError("projection unavailable")
        monkeypatch.setattr(logs.audits, "_index", fail)
        identity = await logs.audit("mcp.create", None, {"mcp_id": "recover"})
        assert identity and logs.audits.index_failures == 1
        monkeypatch.setattr(logs.audits, "_index", original)
        assert (await logs.audits.detail(identity))["target_id"] == "recover"
    finally:
        await logs.close()


async def test_failed_projection_cannot_allow_untracked_audit_deletion(tmp_path, monkeypatch):
    logs = LogStore(tmp_path)
    original = logs.audits._index
    identity = await logs.audit("mcp.create", None, {"mcp_id": "keep"})
    await logs.audit("mcp.create", None, {"mcp_id": "second"})
    try:
        def fail(*args):
            raise RuntimeError("projection unavailable")
        monkeypatch.setattr(logs.audits, "_index", fail)
        with pytest.raises(RuntimeError):
            await logs.audits.delete([identity], actor={"user_id": "admin"})
        monkeypatch.setattr(logs.audits, "_index", original)
        assert await logs.audits.detail(identity)
    finally:
        await logs.close()


async def test_cancellation_waits_for_audit_file_mutation_to_finish(tmp_path, monkeypatch):
    import asyncio
    import threading

    logs = LogStore(tmp_path)
    started, release = threading.Event(), threading.Event()
    identity = await logs.audit("mcp.create", None, {"mcp_id": "cancel"})
    original = logs.audits._rewrite_targets
    def paused(operation_id):
        started.set()
        assert release.wait(5)
        return original(operation_id)
    monkeypatch.setattr(logs.audits, "_rewrite_targets", paused)
    task = asyncio.create_task(logs.audits.delete([identity], actor={"user_id": "admin"}))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await logs.audits.detail(identity) is None
    finally:
        release.set()
        await logs.close()


async def test_malformed_record_diagnostics_survive_process_restart(tmp_path):
    root = tmp_path / "logs" / "audit"
    root.mkdir(parents=True)
    (root / "2026-10-27.jsonl").write_text("broken-json\n", encoding="utf-8")
    logs = LogStore(tmp_path)
    assert (await logs.audits.status())["skipped_records"] == 1
    await logs.close()
    logs = LogStore(tmp_path)
    try:
        assert (await logs.audits.status())["skipped_records"] == 1
    finally:
        await logs.close()
