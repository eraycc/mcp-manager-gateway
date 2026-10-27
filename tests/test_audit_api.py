import asyncio
import csv
import io

import pytest


async def finished(app, response):
    assert response.status_code == 200, response.text
    identity = response.json()["id"]
    for _ in range(200):
        state = await app.app.state.jobs.store.get_status(identity)
        if state["status"] in {"completed", "completed_with_errors", "cancelled", "interrupted"}:
            assert state["status"] == "completed", state
            return state
        await asyncio.sleep(.02)
    raise AssertionError("audit job did not finish")


async def test_audit_api_queries_exports_and_snapshot_delete(db_app):
    web = db_app.web
    logs = db_app.app.state.logs
    a = await logs.audit("mcp.create", db_app.actor["id"], {"mcp_id": "a"}, username="admin")
    b = await logs.audit("mcp.update", None, {"mcp_id": "b"})
    response = await web.get("/api/v1/audit-logs", params={"action": "mcp.create"})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["event_id"] == a
    assert (await web.get("/api/v1/audit-logs/" + a)).json()["target_id"] == "a"
    result = await web.get("/api/v1/audit-logs/export", params={"action": "mcp.create"})
    assert result.status_code == 200 and [e["event_id"] for e in result.json()] == [a]
    result = await web.get("/api/v1/audit-logs/export", params={"format": "csv", "action": "mcp.update"})
    rows = list(csv.DictReader(io.StringIO(result.text.lstrip("\ufeff"))))
    assert len(rows) == 1 and rows[0]["event_id"] == b
    await finished(db_app, await web.post("/api/v1/audit-logs/delete", json={"ids": [a]}))
    assert (await web.get("/api/v1/audit-logs/" + a)).status_code == 404
    assert (await web.get("/api/v1/audit-logs/" + b)).status_code == 200
    await finished(db_app, await web.post("/api/v1/audit-logs/rebuild"))
    assert (await web.get("/api/v1/audit-logs/" + a)).status_code == 404
    assert (await web.get("/api/v1/audit-logs/status")).json()["events"] >= 1


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 201}, {"from_time": "bad"},
                                   {"result": "bogus"}, {"bad_filter": "x"}])
async def test_audit_query_rejects_invalid_filters(db_app, params):
    assert (await db_app.web.get("/api/v1/audit-logs", params=params)).status_code == 422


async def test_audit_clear_requires_confirmation_and_keeps_its_own_record(db_app):
    web = db_app.web
    assert (await web.delete("/api/v1/audit-logs")).status_code == 422
    await finished(db_app, await web.delete("/api/v1/audit-logs?confirm=true"))
    rows = (await web.get("/api/v1/audit-logs")).json()["items"]
    assert rows and all(row["action"] == "audit.clear" for row in rows)
    phases = [(await web.get("/api/v1/audit-logs/" + row["event_id"])).json()["details"]["phase"] for row in rows]
    assert "completed" in phases


async def test_audit_is_admin_only_for_all_operations(db_app):
    web = db_app.web
    response = await web.post("/api/v1/auth/register", json={"username": "ordinary", "password": "password12345"})
    assert response.status_code == 200
    await web.post("/api/v1/auth/login", json={"username": "ordinary", "password": "password12345"})
    web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
    for method, path, kwargs in [
        ("get", "/audit-logs", {}), ("get", "/audit-logs/status", {}), ("get", "/audit-logs/actions", {}),
        ("get", "/audit-logs/export", {}), ("get", "/audit-logs/missing", {}),
        ("post", "/audit-logs/delete", {"json": {"ids": ["x"]}}),
        ("delete", "/audit-logs?confirm=true", {}), ("post", "/audit-logs/rebuild", {}),
    ]:
        assert (await getattr(web, method)("/api/v1" + path, **kwargs)).status_code == 403


async def test_queued_audit_delete_revalidates_administrator_before_writing(db_app, monkeypatch):
    from fastapi import HTTPException

    from mcp_manager.database import User

    captured = {}
    async def capture(action, items, operation, user_id):
        captured["operation"] = operation
        return {"id": "queued"}
    logs = db_app.app.state.logs
    identity = await logs.audit("mcp.create", None, {"mcp_id": "keep"})
    monkeypatch.setattr(db_app.app.state.jobs, "submit", capture)
    response = await db_app.web.post("/api/v1/audit-logs/delete", json={"ids": [identity]})
    assert response.status_code == 200
    async with db_app.db.locked() as session:
        user = await session.get(User, db_app.actor["id"])
        user.role = "user"
    with pytest.raises(HTTPException):
        await captured["operation"]("audit")
    assert await logs.audits.detail(identity)


async def test_audit_export_escapes_spreadsheet_formulas(db_app):
    await db_app.app.state.logs.audit("mcp.create", None, {}, target_name="=HYPERLINK(attacker)")
    response = await db_app.web.get("/api/v1/audit-logs/export?format=csv")
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert any(row["target_name"] == "'=HYPERLINK(attacker)" for row in rows)

async def test_mcp_names_resolve_for_legacy_audits_without_overwriting_snapshots(db_app):
    from mcp_manager.database import McpServer

    async with db_app.db.locked() as session:
        session.add(McpServer(id="named-mcp", slug="named-mcp", name="Current MCP",
                              transport="stdio", config={}, mode="disabled"))
    logs = db_app.app.state.logs
    legacy = await logs.audit("oauth.refresh", None, {"mcp_id": "named-mcp"})
    snapshot = await logs.audit("mcp.update", None, {"mcp_id": "named-mcp"}, target_name="Original MCP")
    missing = await logs.audit("oauth.refresh", None, {"mcp_id": "removed-mcp"})
    web = db_app.web
    rows = (await web.get("/api/v1/audit-logs")).json()["items"]
    names = {row["event_id"]: row["target_name"] for row in rows}
    assert names[legacy] == "Current MCP"
    assert names[snapshot] == "Original MCP"
    assert not names[missing]
    assert (await web.get("/api/v1/audit-logs/" + legacy)).json()["target_name"] == "Current MCP"
    exported = (await web.get("/api/v1/audit-logs/export?action=oauth.refresh")).json()
    assert next(row for row in exported if row["event_id"] == legacy)["target_name"] == "Current MCP"
    # Enrichment is read-only: authoritative records retain their original metadata.
    assert not (await logs.audits.detail(legacy))["target_name"]
    async with db_app.db.locked() as session:
        await session.delete(await session.get(McpServer, "named-mcp"))
    assert not (await web.get("/api/v1/audit-logs/" + legacy)).json()["target_name"]


async def test_log_retention_settings_are_independent_and_default_to_unlimited(db_app):
    web = db_app.web
    settings = (await web.get("/api/v1/settings")).json()
    assert settings["log_retention_days"] == 0
    assert settings["audit_log_retention_days"] == 0
    response = await web.patch("/api/v1/settings", json={"log_retention_days": 7})
    assert response.status_code == 200
    assert response.json()["audit_log_retention_days"] == 0
    assert (await web.get("/api/v1/audit-logs/status")).json()["retention_days"] == 0
    db_app.app.state.retention_next = float("inf")
    response = await web.patch("/api/v1/settings", json={"audit_log_retention_days": 30})
    assert response.status_code == 200
    assert response.json()["log_retention_days"] == 7
    assert db_app.app.state.retention_next == 0
    assert (await web.get("/api/v1/audit-logs/status")).json()["retention_days"] == 30
    for invalid in [-1, True, "7"]:
        assert (await web.patch("/api/v1/settings", json={"audit_log_retention_days": invalid})).status_code == 422
