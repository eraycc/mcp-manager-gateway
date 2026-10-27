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
