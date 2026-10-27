async def events(app, action):
    return (await app.web.get("/api/v1/audit-logs", params={"action": action})).json()["items"]


async def test_identity_and_token_events_are_committed_named_and_secret_free(db_app):
    web = db_app.web
    failed = await web.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-private"})
    assert failed.status_code == 401
    assert await events(db_app, "identity.login_failed")
    created = (await web.post("/api/v1/tokens", json={"name": "Audited token", "scope_mode": "all"})).json()
    identity, secret = created["id"], created["token"]
    assert (await web.patch("/api/v1/tokens/" + identity, json={"name": "Renamed token"})).status_code == 200
    assert (await web.get("/api/v1/tokens/" + identity + "/secret")).status_code == 200
    rotated = (await web.post("/api/v1/tokens/" + identity + "/rotate")).json()["token"]
    assert (await web.delete("/api/v1/tokens/" + identity)).status_code == 200
    for action in ["identity.login", "token.create", "token.update", "token.secret.reveal", "token.rotate", "token.delete"]:
        rows = await events(db_app, action)
        assert rows, action
        assert rows[0]["username"] == "admin"
        assert rows[0]["source"] == "web"
        assert rows[0]["ip"] and rows[0]["request_id"]
        if action.startswith("token."):
            assert rows[0]["target_id"] == identity
    all_events = (await web.get("/api/v1/audit-logs/export")).text
    assert secret not in all_events and rotated not in all_events and "wrong-private" not in all_events


async def test_user_permission_and_settings_diff_events(db_app):
    web = db_app.web
    response = await web.post("/api/v1/users", json={"username": "audit-user", "password": "password12345", "role": "user"})
    assert response.status_code == 200
    identity = response.json()["id"]
    response = await web.patch("/api/v1/users/" + identity, json={"role": "admin"})
    assert response.status_code == 200
    assert (await events(db_app, "user.role_change"))[0]["target_id"] == identity
    assert (await web.delete("/api/v1/users/" + identity)).status_code == 200
    assert await events(db_app, "user.delete")
    assert (await web.patch("/api/v1/settings", json={"title": "Audited title"})).status_code == 200
    row = (await events(db_app, "settings.update"))[0]
    detail = (await web.get("/api/v1/audit-logs/" + row["event_id"])).json()
    assert set(detail["details"]["changes"]) == {"title"}
    assert detail["details"]["changes"]["title"] == {"old": "MCP Manager", "new": "Audited title"}


async def test_completed_settings_change_survives_audit_disk_failure(db_app, monkeypatch):
    def fail(event):
        raise OSError("simulated full disk")
    monkeypatch.setattr(db_app.app.state.logs.audits, "_write", fail)
    response = await db_app.web.patch("/api/v1/settings", json={"title": "Saved despite audit outage"})
    assert response.status_code == 200
    assert (await db_app.web.get("/api/v1/settings")).json()["title"] == "Saved despite audit outage"
    status = (await db_app.web.get("/api/v1/audit-logs/status")).json()
    assert status["write_failures"] > 0 and status["last_error"]["type"] == "OSError"


async def test_service_crud_and_runtime_results_are_recorded_once(db_app, monkeypatch):
    cat = db_app.app.state.catalog
    row = await cat.create({"name": "Audit service", "slug": "audit-service", "transport": "stdio",
                            "mode": "disabled", "config": {"command": "never-launched"}},
                           user_id=db_app.actor["id"], discover=False)
    response = await db_app.web.patch("/api/v1/mcps/" + row.id, json={"description": "Audit changed"})
    assert response.status_code == 200
    assert len(await events(db_app, "mcp.update")) == 1
    async def warm(*args, **kwargs):
        return None
    monkeypatch.setattr(cat, "retry_disabled", warm)
    monkeypatch.setattr(cat, "warm", warm)
    assert (await db_app.web.post("/api/v1/mcps/" + row.id + "/start")).status_code == 200
    assert (await events(db_app, "mcp.start"))[0]["result"] == "success"
    assert (await db_app.web.post("/api/v1/mcps/" + row.id + "/stop")).status_code == 200
    assert await events(db_app, "mcp.stop")
    assert (await db_app.web.delete("/api/v1/mcps/" + row.id)).status_code == 200
    assert len(await events(db_app, "mcp.delete")) == 1


async def test_proposal_events_cover_gateway_submission_rejection_and_deletion(db_app):
    from test_proposal_atomicity import propose

    identity = await propose(db_app, name="Audited proposal")
    assert await events(db_app, "mcp.proposal.submit")
    response = await db_app.web.post("/api/v1/mcp-proposals/" + identity + "/reject", json={"reason": "Do not install"})
    assert response.status_code == 200
    assert await events(db_app, "mcp.proposal.reject")
    assert (await db_app.web.delete("/api/v1/mcp-proposals/" + identity)).status_code == 200
    assert await events(db_app, "mcp.proposal.delete")
