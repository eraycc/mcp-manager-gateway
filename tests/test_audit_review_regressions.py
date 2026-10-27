"""Counterexamples from the independent review, using only temporary data."""
import importlib
import json

import pytest

from mcp_manager.audit_events import build_event
from mcp_manager.logs import LogStore


async def test_accepted_settings_urls_never_persist_query_credentials_in_audit(db_app):
    web = db_app.web
    secret = "review-private-placeholder"
    for key in ["refresh_token", "id_token", "client_secret", "registration_access_token",
                "%72efresh_token", "client%5Fsecret", "apiKey"]:
        config = (await web.get("/api/v1/settings")).json()["translation_config"]
        config["custom_host"] = "https://example.test/?scope=visible&" + key + "=" + secret
        response = await web.patch("/api/v1/settings", json={"translation_config": config})
        assert response.status_code == 200
        page = (await web.get("/api/v1/audit-logs?action=settings.update")).json()
        identity = page["items"][0]["event_id"]
        detail = (await web.get("/api/v1/audit-logs/" + identity)).json()
        assert secret not in json.dumps(detail)
        assert "scope=visible" in json.dumps(detail)
    exported = await web.get("/api/v1/audit-logs/export")
    assert secret not in exported.text
    original = "".join(path.read_text(encoding="utf-8") for path in db_app.app.state.logs.audits.root.glob("*.jsonl"))
    assert secret not in original


def test_audit_url_userinfo_and_fragment_secrets_are_redacted():
    event = build_event("settings.update", None, {"changes": {
        "url": {"old": "https://old-user:private-pass@example.test/",
                "new": "https://example.test/#refresh_token=private-fragment&scope=visible"}}})
    raw = json.dumps(event)
    assert "private-pass" not in raw and "old-user" not in raw and "private-fragment" not in raw
    assert "scope=visible" in raw


@pytest.mark.parametrize("bad", [
    {"actor_type": {"x": 1}}, {"username": ["unexpected"]}, {"target_name": {"value": "unexpected"}},
    {"source": ["web"]}, {"timestamp": None}, {"action": {"name": "unexpected"}}, {"details": ["unexpected"]},
])
async def test_malformed_metadata_is_skipped_without_breaking_startup_or_restart(tmp_path, bad):
    root = tmp_path / "logs" / "audit"
    root.mkdir(parents=True)
    event = {"event_id": "bad", "timestamp": "2026-10-27T01:00:00Z", "action": "mcp.create",
             "user_id": None, "details": {"mcp_id": "x"}}
    rows = [event | {"event_id": "before"}, event | bad, event | {"event_id": "after"}]
    original = "".join(json.dumps(row) + "\n" for row in rows)
    path = root / "2026-10-27.jsonl"
    path.write_text(original, encoding="utf-8")
    for _ in range(2):
        logs = LogStore(tmp_path)
        try:
            assert (await logs.audits.query())["total"] == 2
            assert (await logs.audits.status())["skipped_records"] == 1
            assert path.read_text(encoding="utf-8") == original
        finally:
            await logs.close()


async def test_real_startup_and_maintenance_failures_appear_in_failure_filter(db_app, monkeypatch):
    app_module = importlib.import_module("mcp_manager.app")
    cat = db_app.app.state.catalog
    row = await cat.create({"name": "Failed audit startup", "slug": "failed-audit-startup", "mode": "lazy",
                            "transport": "stdio", "config": {"command": "synthetic-never-launched"}},
                           user_id=db_app.actor["id"], discover=False)
    async def fail(*args, **kwargs):
        raise OSError("synthetic failure")
    monkeypatch.setattr(db_app.app.state.runtime, "perform", fail)
    with pytest.raises(OSError):
        await cat.warm(row.id, db_app.actor["id"], explicit=True)
    monkeypatch.setattr(app_module, "MAINTENANCE_STEPS", (fail,))
    await app_module._maintenance_cycle(db_app.app)
    rows = (await db_app.web.get("/api/v1/audit-logs?result=failure")).json()["items"]
    assert {"mcp.start.failed", "maintenance.error"} <= {row["action"] for row in rows}
