import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import Request
from sqlalchemy import select

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import McpServer, SystemSetting, User, get_setting
from mcp_manager.identity import admin_user, current_user


@pytest.fixture
async def web(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, public_url="http://test"))
    ready, finished = asyncio.Event(), asyncio.Event()

    async def lifespan_owner():
        async with app.router.lifespan_context(app):
            ready.set()
            await finished.wait()

    owner = asyncio.create_task(lifespan_owner())
    await asyncio.wait_for(ready.wait(), 10)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            actor = (await c.post("/api/v1/auth/register", json={
                "username": "admin", "password": "password123"})).json()
            await c.post("/api/v1/auth/login", json={"username": "admin", "password": "password123"})
            c.headers["X-CSRF-Token"] = c.cookies["mcp_csrf"]
            row = await app.state.catalog.create({
                "name": "Existing", "slug": "existing", "transport": "stdio",
                "config": {"command": "must-never-be-launched"}, "mode": "disabled"})
            yield app, c, actor, row
    finally:
        finished.set()
        await asyncio.wait_for(owner, 10)


async def test_test_case_update_invalidates_setting_cache(web):
    app, client, _actor, row = web
    path = f"/api/v1/mcps/{row.id}/test-cases"
    first = [{"tool": "one"}]
    second = [{"tool": "two"}]

    assert (await client.put(path, json={"cases": first})).status_code == 200
    assert await get_setting(app.state.db, "tests:" + row.id, []) == first
    assert (await client.put(path, json={"cases": second})).status_code == 200
    assert await get_setting(app.state.db, "tests:" + row.id, []) == second


@pytest.mark.parametrize("operation", [
    "create", "update", "delete", "copy", "import", "settings", "test_cases",
    "oauth_start", "oauth_disconnect", "start", "stop", "refresh", "diagnose", "test",
    "batch", "test_all", "logs_delete", "logs_rebuild",
])
async def test_web_mutations_recheck_detached_actor(web, operation):
    app, c, actor, row = web
    routes = {
        "create": ("POST", "/mcps", {"name": "Injected", "config": {"command": "never"}}),
        "update": ("PATCH", "/mcps/" + row.id, {"name": "Changed"}),
        "delete": ("DELETE", "/mcps/" + row.id, None),
        "copy": ("POST", "/mcps/" + row.id + "/copy", None),
        "import": ("POST", "/mcps/import", {"data": {"mcpServers": {
            "injected": {"command": "never"}}}}),
        "settings": ("PATCH", "/settings", {"title": "Changed"}),
        "test_cases": ("PUT", "/mcps/" + row.id + "/test-cases", {"cases": [{"tool": "x"}]}),
        "oauth_start": ("POST", "/mcps/" + row.id + "/oauth/start", None),
        "oauth_disconnect": ("POST", "/mcps/" + row.id + "/oauth/disconnect", None),
        "start": ("POST", "/mcps/" + row.id + "/start", None),
        "stop": ("POST", "/mcps/" + row.id + "/stop", None),
        "refresh": ("POST", "/mcps/" + row.id + "/refresh", None),
        "diagnose": ("POST", "/mcps/diagnose", {"config": {"command": "never"}}),
        "test": ("POST", "/mcps/" + row.id + "/test", {"tool": "x"}),
        "batch": ("POST", "/mcps/batch", {"action": "delete", "ids": [row.id]}),
        "test_all": ("POST", "/mcps/" + row.id + "/test-all", None),
        "logs_delete": ("POST", "/logs/delete", {"ids": []}),
        "logs_rebuild": ("POST", "/logs/rebuild", None),
    }
    entered, release = asyncio.Event(), asyncio.Event()

    async def stale_actor(request: Request):
        user = await current_user(request)
        entered.set()
        await release.wait()
        return user

    app.dependency_overrides[admin_user] = stale_actor
    app.dependency_overrides[current_user] = stale_actor
    method, path, data = routes[operation]
    task = asyncio.create_task(c.request(method, "/api/v1" + path, json=data))
    await asyncio.wait_for(entered.wait(), 5)
    async with app.state.db.locked() as s:
        (await s.get(User, actor["id"])).disabled = True
    release.set()
    response = await asyncio.wait_for(task, 10)
    assert response.status_code == 401, response.text
    async with app.state.db.session() as s:
        saved = await s.get(McpServer, row.id)
        assert saved is not None and saved.name == "Existing" and saved.revision == 1
        assert len((await s.scalars(select(McpServer))).all()) == 1
        assert await s.get(SystemSetting, "title") is None
        assert await s.get(SystemSetting, "tests:" + row.id) is None
    assert app.state.jobs.items == {}


@pytest.mark.parametrize("action", ["batch", "test_all", "logs_delete", "logs_rebuild"])
async def test_queued_web_jobs_revalidate_before_execution(web, action):
    app, c, actor, row = web
    # The real job semaphore is a deterministic queue barrier, not a mocked job.
    app.state.jobs.limit = asyncio.Semaphore(0)
    if action == "batch":
        response = await c.post("/api/v1/mcps/batch", json={"action": "delete", "ids": [row.id]})
    elif action == "test_all":
        await c.put("/api/v1/mcps/" + row.id + "/test-cases", json={"cases": [{"tool": "x"}]})
        response = await c.post("/api/v1/mcps/" + row.id + "/test-all")
    else:
        response = await c.post("/api/v1/logs/" + action.removeprefix("logs_"), json={"ids": []})
    assert response.status_code == 200, response.text
    job = response.json()
    async with app.state.db.locked() as s:
        (await s.get(User, actor["id"])).auth_version += 1
    app.state.jobs.limit.release()
    await asyncio.wait_for(app.state.jobs.tasks[job["id"]], 10)
    result = app.state.jobs.items[job["id"]]
    assert result["status"] == "completed_with_errors"
    assert "Session revoked" in result["results"][0]["error"]
    assert await app.state.catalog.get(row.id)


@pytest.mark.parametrize("change", ["session", "grant", "revision", "service_role"])
async def test_oauth_callback_rechecks_after_network_without_holding_db_lock(web, monkeypatch, change):
    app, c, actor, row = web
    config = {"url": "https://example.com/mcp", "auth": {
        "type": "oauth", "scope": "user" if change == "grant" else "service",
        "authorization_url": "https://example.com/authorize", "token_url": "https://example.com/token",
        "client_id": "test"}}
    async with app.state.db.locked() as s:
        saved = await s.get(McpServer, row.id)
        saved.transport = "streamable-http"
        saved.config = app.state.catalog.seal(config)
        if change == "grant":
            saved.isolation = "user"
            user = await s.get(User, actor["id"])
            user.role, user.mcp_ids = "user", [row.id]
    response = await c.post("/api/v1/mcps/" + row.id + "/oauth/start")
    assert response.status_code == 200, response.text
    state = parse_qs(urlparse(response.json()["authorization_url"]).query)["state"][0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def exchange(auth, data):
        entered.set()
        await release.wait()
        return {"access_token": "must-not-be-stored"}

    monkeypatch.setattr(app.state.oauth, "exchange", exchange)
    task = asyncio.create_task(c.get("/api/v1/oauth/callback", params={"state": state, "code": "test"}))
    await asyncio.wait_for(entered.wait(), 5)

    async def revoke():
        async with app.state.db.locked() as s:
            user = await s.get(User, actor["id"])
            if change == "session":
                user.auth_version += 1
            elif change == "grant":
                user.mcp_ids = []
            elif change == "service_role":
                user.role = "user"
            else:
                (await s.get(McpServer, row.id)).revision += 1

    await asyncio.wait_for(revoke(), 3)
    release.set()
    response = await asyncio.wait_for(task, 10)
    assert response.status_code == {"session": 401, "grant": 403, "revision": 409, "service_role": 403}[change]
    async with app.state.db.session() as s:
        assert (await s.scalars(select(SystemSetting).where(SystemSetting.key.like("oauth:%")))).all() == []


@pytest.mark.parametrize("action", ["update", "delete"])
async def test_catalog_revalidates_inside_write_after_downstream_stop(web, monkeypatch, action):
    app, c, actor, row = web
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.state.runtime.stop_server

    async def delayed_stop(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.state.runtime, "stop_server", delayed_stop)
    if action == "update":
        task = asyncio.create_task(c.patch("/api/v1/mcps/" + row.id, json={"name": "Changed"}))
    else:
        task = asyncio.create_task(c.delete("/api/v1/mcps/" + row.id))
    await asyncio.wait_for(entered.wait(), 5)

    async def revoke():
        async with app.state.db.locked() as s:
            (await s.get(User, actor["id"])).auth_version += 1

    # Also proves downstream network/drain work does not hold the DB mutex.
    await asyncio.wait_for(revoke(), 3)
    release.set()
    response = await asyncio.wait_for(task, 5)
    assert response.status_code == 401, response.text
    saved = await app.state.catalog.get(row.id)
    assert saved.name == "Existing" and saved.revision == 1


async def test_job_cancel_uses_fresh_ownership(web):
    app, c, actor, row = web
    app.state.jobs.limit = asyncio.Semaphore(0)
    queued = app.state.jobs.submit("other-user-job", [1], lambda item: None, user_id="other-user")
    entered, release = asyncio.Event(), asyncio.Event()

    async def stale_actor(request: Request):
        user = await current_user(request)
        entered.set()
        await release.wait()
        return user

    app.dependency_overrides[current_user] = stale_actor
    task = asyncio.create_task(c.post("/api/v1/jobs/" + queued["id"] + "/cancel"))
    await asyncio.wait_for(entered.wait(), 5)
    async with app.state.db.locked() as s:
        (await s.get(User, actor["id"])).role = "user"
    release.set()
    response = await asyncio.wait_for(task, 5)
    assert response.status_code == 404, response.text
    assert not app.state.jobs.tasks[queued["id"]].done()


async def test_oauth_callback_rechecks_before_token_exchange(web, monkeypatch):
    app, c, actor, row = web
    async with app.state.db.locked() as s:
        saved = await s.get(McpServer, row.id)
        saved.config = app.state.catalog.seal({"command": "never", "auth": {
            "type": "oauth", "scope": "service", "client_id": "test",
            "authorization_url": "https://example.com/authorize", "token_url": "https://example.com/token"}})
    response = await c.post("/api/v1/mcps/" + row.id + "/oauth/start")
    state = parse_qs(urlparse(response.json()["authorization_url"]).query)["state"][0]
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def stale_actor(request: Request):
        user = await current_user(request)
        entered.set()
        await release.wait()
        return user

    async def exchange(auth, data):
        calls.append(data)
        return {"access_token": "forbidden"}

    app.dependency_overrides[current_user] = stale_actor
    monkeypatch.setattr(app.state.oauth, "exchange", exchange)
    task = asyncio.create_task(c.get("/api/v1/oauth/callback", params={"state": state, "code": "test"}))
    await asyncio.wait_for(entered.wait(), 5)
    async with app.state.db.locked() as s:
        (await s.get(User, actor["id"])).disabled = True
    release.set()
    response = await asyncio.wait_for(task, 5)
    assert response.status_code == 401
    assert calls == []
