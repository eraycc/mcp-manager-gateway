import asyncio

import httpx
import pytest
from fastapi import FastAPI
from mcp_manager.config import Settings
from mcp_manager.database import Database, McpServer, get_setting, set_setting
from mcp_manager.identity import router, authenticate_token


@pytest.fixture
async def clients(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path}/test.db")
    db = Database(settings)
    await db.initialize()
    app = FastAPI()
    app.state.db, app.state.config = db, settings
    app.include_router(router)

    @app.get("/probe")
    async def probe(request: __import__("fastapi").Request):
        user, token, ids = await authenticate_token(request)
        return {"ids": sorted(ids)}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as a:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as b:
            yield a, b, db
    await db.close()


async def signup(c, name):
    r = await c.post("/api/v1/auth/register", json={"username": name, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()


async def login(c, name):
    r = await c.post("/api/v1/auth/login", json={"username": name, "password": "password123"})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = c.cookies["mcp_csrf"]


async def test_first_registration_race_and_registration_off(clients):
    a, b, db = clients
    users = await asyncio.gather(signup(a, "alice"), signup(b, "bobby"))
    assert sorted(u["role"] for u in users) == ["admin", "user"]
    assert all("password_hash" not in u for u in users)
    await set_setting(db, "registration_enabled", False)
    assert (await a.post("/api/v1/auth/register", json={"username":"third","password":"password123"})).status_code == 403


async def test_cookie_csrf_last_admin_and_session_revocation(clients):
    a, b, db = clients
    admin = await signup(a, "admin")
    await set_setting(db, "jwt_days", 0)
    await login(a, "admin")
    assert (await a.get("/api/v1/me")).status_code == 200
    assert (await a.patch("/api/v1/me", headers={"X-CSRF-Token": ""}, json={"email":"a@b.com"})).status_code == 403
    assert (await a.delete("/api/v1/users/"+admin["id"])).status_code == 409
    await login(b, "admin")
    assert (await a.patch("/api/v1/me", json={"current_password":"password123","password":"newpassword123"})).status_code == 200
    assert (await b.get("/api/v1/me")).status_code == 401
    assert (await a.post("/api/v1/auth/logout")).status_code == 200
    assert (await a.get("/api/v1/me")).status_code == 401


async def test_user_delete_removes_only_personal_credential_settings(clients):
    a, b, db = clients
    await signup(a, "admin")
    target = await signup(b, "target")
    await login(a, "admin")
    await set_setting(db, f"credential:server:{target['id']}", {"sealed": "personal"})
    await set_setting(db, f"oauth:server:{target['id']}", {"sealed": "legacy"})
    await set_setting(db, "credential:server:service", {"sealed": "global"})

    response = await a.delete("/api/v1/users/" + target["id"])

    assert response.status_code == 200
    assert await get_setting(db, f"credential:server:{target['id']}") is None
    assert await get_setting(db, f"oauth:server:{target['id']}") is None
    assert await get_setting(db, "credential:server:service") == {"sealed": "global"}


async def test_token_scope_revocation_and_object_access(clients):
    a,b,db = clients
    await signup(a,"admin")
    user = await signup(b,"plain")
    await login(a,"admin")
    await login(b,"plain")
    async with db.session() as s:
        server = McpServer(slug="one", name="One", transport="stdio", config={})
        s.add(server)
        await s.commit()
        sid=server.id
    r=await b.post("/api/v1/tokens",json={"name":"bad","scope_mode":"selected","mcp_ids":[sid]})
    assert r.status_code == 403
    assert (await b.patch("/api/v1/users/"+user["id"],json={"role":"admin"})).status_code == 403
    assert (await a.patch("/api/v1/users/"+user["id"],json={"mcp_ids":[sid]})).status_code == 200
    r=await b.post("/api/v1/tokens",json={"name":"ok","scope_mode":"all"})
    assert r.status_code == 200,r.text
    token=r.json()
    assert "token_hash" not in token
    headers={"Authorization":"Bearer "+token["token"]}
    assert (await a.get("/probe",headers=headers)).json()["ids"] == [sid]
    assert (await b.patch("/api/v1/tokens/"+token["id"],json={"disabled":True})).status_code == 200
    assert (await a.get("/probe",headers=headers)).status_code == 401
    await set_setting(db,"token_auth_enabled",False)
    assert (await a.get("/probe",headers=headers)).status_code == 401
    assert (await a.get("/probe")).json()["ids"] == []


async def test_rotation_owner_isolation_and_live_permission_reduction(clients):
    a,b,db=clients
    admin=await signup(a,"admin")
    await signup(b,"other")
    await login(a,"admin")
    await login(b,"other")
    t=(await a.post("/api/v1/tokens",json={"name":"admin-token","scope_mode":"all"})).json()
    for method,path,payload in [
        ("PATCH","/api/v1/tokens/"+t["id"],{"name":"stolen"}),
        ("DELETE","/api/v1/tokens/"+t["id"],None),
        ("POST","/api/v1/tokens/"+t["id"]+"/rotate",None),
    ]:
        assert (await b.request(method,path,json=payload)).status_code == 404
    assert (await b.post("/api/v1/tokens",json={"name":"stolen","user_id":admin["id"]})).status_code == 403
    rotated=(await a.post("/api/v1/tokens/"+t["id"]+"/rotate")).json()
    assert (await a.get("/probe",headers={"Authorization":"Bearer "+t["token"]})).status_code == 401
    assert (await a.get("/probe",headers={"Authorization":"Bearer "+rotated["token"]})).status_code == 200
    assert "token" not in (await a.get("/api/v1/tokens")).json()["items"][0]
    sessions=(await a.get("/api/v1/me/sessions")).json()
    assert (await b.delete("/api/v1/me/sessions/"+sessions[0]["id"])).status_code == 404
    assert (await a.delete("/api/v1/me/sessions/"+sessions[0]["id"])).status_code == 200
    assert (await a.get("/api/v1/me")).status_code == 401


async def test_origin_validation_and_last_admin_batch_rollback(clients):
    a,b,db=clients
    assert (await a.post("/api/v1/auth/register",headers={"Origin":"https://evil.example"},
                        json={"username":"admin","password":"password123"})).status_code == 403
    admin=await signup(a,"admin")
    ordinary=await signup(b,"other")
    await login(a,"admin")
    r=await a.post("/api/v1/users/batch",json={"action":"delete","ids":[admin["id"],ordinary["id"]]})
    assert r.status_code == 409
    assert (await a.get("/api/v1/users")).json()["total"] == 2
    assert (await a.patch("/api/v1/users/"+admin["id"],json={"role":"user"})).status_code == 409
    assert (await a.get("/api/v1/me")).json()["role"] == "admin"


async def test_expiry_honors_explicit_timezone_offset(clients):
    from datetime import datetime, timedelta, timezone
    a,b,db=clients
    await signup(a,"admin")
    await login(a,"admin")
    past=(datetime.now(timezone.utc)-timedelta(minutes=1)).astimezone(timezone(timedelta(hours=8)))
    r=await a.post("/api/v1/tokens",json={"name":"expired","expires_at":past.isoformat()})
    assert r.status_code == 200,r.text
    assert (await a.get("/probe",headers={"Authorization":"Bearer "+r.json()["token"]})).status_code == 401


@pytest.mark.parametrize("operation", [
    "logout", "profile", "session", "user_create", "user_batch", "user_patch",
    "user_delete", "token_create", "token_patch", "token_delete", "token_rotate",
])
async def test_writes_revalidate_revoked_session_after_lock_wait(clients, monkeypatch, operation):
    from contextlib import asynccontextmanager
    from sqlalchemy import select
    from mcp_manager.database import ApiToken, AuthSession, User

    a, b, db = clients
    actor = await signup(a, "admin")
    target = await signup(b, "target")
    await login(a, "admin")
    token = (await a.post("/api/v1/tokens", json={"name": "original"})).json()
    sid = (await a.get("/api/v1/me/sessions")).json()[0]["id"]
    routes = {
        "logout": ("POST", "/auth/logout", None),
        "profile": ("PATCH", "/me", {"email": "changed@example.com"}),
        "session": ("DELETE", "/me/sessions/" + sid, None),
        "user_create": ("POST", "/users", {"username": "injected", "password": "password123"}),
        "user_batch": ("POST", "/users/batch", {"action": "disable", "ids": [target["id"]]}),
        "user_patch": ("PATCH", "/users/" + target["id"], {"role": "admin"}),
        "user_delete": ("DELETE", "/users/" + target["id"], None),
        "token_create": ("POST", "/tokens", {"name": "injected"}),
        "token_patch": ("PATCH", "/tokens/" + token["id"], {"name": "changed"}),
        "token_delete": ("DELETE", "/tokens/" + token["id"], None),
        "token_rotate": ("POST", "/tokens/" + token["id"] + "/rotate", None),
    }
    original_lock = db.locked
    entered = asyncio.Event()

    @asynccontextmanager
    async def observed_lock():
        if asyncio.current_task().get_name() == "stale-request":
            entered.set()
        async with original_lock() as s:
            yield s

    monkeypatch.setattr(db, "locked", observed_lock)
    method, path, payload = routes[operation]
    async with original_lock() as s:
        task = asyncio.create_task(a.request(method, "/api/v1" + path, json=payload), name="stale-request")
        await asyncio.wait_for(entered.wait(), 5)
        session = await s.get(AuthSession, sid)
        session.revoked = True
    response = await asyncio.wait_for(task, 5)
    assert response.status_code == 401, response.text
    async with db.session() as s:
        assert (await s.get(User, actor["id"])).email == ""
        remaining = await s.get(User, target["id"])
        assert remaining is not None and remaining.role == "user" and not remaining.disabled
        assert await s.scalar(select(User).where(User.username == "injected")) is None
        rows = (await s.scalars(select(ApiToken))).all()
        assert len(rows) == 1 and rows[0].name == "original"
        assert rows[0].prefix == token["prefix"]


@pytest.mark.parametrize("change", ["demote", "disable", "auth_version"])
async def test_admin_role_and_state_revalidated_under_lock(clients, monkeypatch, change):
    from contextlib import asynccontextmanager
    from mcp_manager.database import User

    a, b, db = clients
    actor = await signup(a, "admin")
    await login(a, "admin")
    original_lock = db.locked
    entered = asyncio.Event()

    @asynccontextmanager
    async def observed_lock():
        if asyncio.current_task().get_name() == "stale-request":
            entered.set()
        async with original_lock() as s:
            yield s

    monkeypatch.setattr(db, "locked", observed_lock)
    async with original_lock() as s:
        task = asyncio.create_task(a.post("/api/v1/users", json={
            "username": "injected", "password": "password123"}), name="stale-request")
        await asyncio.wait_for(entered.wait(), 5)
        row = await s.get(User, actor["id"])
        if change == "demote":
            row.role = "user"
        elif change == "disable":
            row.disabled = True
        else:
            row.auth_version += 1
    response = await asyncio.wait_for(task, 5)
    assert response.status_code == (403 if change == "demote" else 401), response.text


async def test_token_patch_metadata_survives_revoked_grants(clients):
    a, b, db = clients
    await signup(a, "admin")
    owner = await signup(b, "owner")
    await login(a, "admin")
    await login(b, "owner")
    async with db.session() as s:
        row = McpServer(slug="scope", name="Scope", transport="stdio", config={})
        s.add(row)
        await s.commit()
        server_id = row.id
    assert (await a.patch("/api/v1/users/" + owner["id"], json={"mcp_ids": [server_id]})).status_code == 200
    token = (await b.post("/api/v1/tokens", json={"name": "original", "mcp_ids": [server_id]})).json()
    assert (await a.patch("/api/v1/users/" + owner["id"], json={"mcp_ids": []})).status_code == 200
    response = await b.patch("/api/v1/tokens/" + token["id"], json={"name": "renamed", "disabled": True})
    assert response.status_code == 200, response.text
    assert response.json()["disabled"] is True
    assert response.json()["name"] == "renamed"
    assert (await b.patch("/api/v1/tokens/" + token["id"], json={
        "name": "full-form", "mcp_ids": [server_id], "scope_mode": "selected"})).status_code == 200
    assert (await b.patch("/api/v1/tokens/" + token["id"], json={
        "mcp_ids": [server_id, "ungranted-server"]})).status_code == 403


@pytest.mark.parametrize("operation", ["create", "patch", "delete", "rotate"])
async def test_token_ownership_uses_fresh_role_after_lock_wait(clients, monkeypatch, operation):
    from contextlib import asynccontextmanager
    from mcp_manager.database import User

    a, b, db = clients
    actor = await signup(a, "admin")
    owner = await signup(b, "owner")
    await login(a, "admin")
    await login(b, "owner")
    token = (await b.post("/api/v1/tokens", json={"name": "owner-token"})).json()
    routes = {
        "create": ("POST", "/api/v1/tokens", {"name": "injected", "user_id": owner["id"]}),
        "patch": ("PATCH", "/api/v1/tokens/" + token["id"], {"name": "changed"}),
        "delete": ("DELETE", "/api/v1/tokens/" + token["id"], None),
        "rotate": ("POST", "/api/v1/tokens/" + token["id"] + "/rotate", None),
    }
    original_lock = db.locked
    entered = asyncio.Event()

    @asynccontextmanager
    async def observed_lock():
        if asyncio.current_task().get_name() == "stale-request":
            entered.set()
        async with original_lock() as s:
            yield s

    monkeypatch.setattr(db, "locked", observed_lock)
    async with original_lock() as s:
        method, path, payload = routes[operation]
        task = asyncio.create_task(a.request(method, path, json=payload), name="stale-request")
        await asyncio.wait_for(entered.wait(), 5)
        (await s.get(User, actor["id"])).role = "user"
    response = await asyncio.wait_for(task, 5)
    assert response.status_code == (403 if operation == "create" else 404), response.text
    rows = (await b.get("/api/v1/tokens")).json()["items"]
    assert len(rows) == 1 and rows[0]["name"] == "owner-token"
    assert rows[0]["prefix"] == token["prefix"]
