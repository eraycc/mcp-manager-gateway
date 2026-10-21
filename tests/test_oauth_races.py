"""Authorization generations reject stale callbacks and failures, even with identical tokens."""
import asyncio
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from test_oauth_reauth import oauth_row, save_token
from test_oauth_registration import discovery, server

from mcp_manager.database import User
from mcp_manager.runtime import GatewayError


async def test_old_401_cannot_invalidate_a_new_generation_of_the_same_token(db_app):
    row, user = await oauth_row(db_app)
    token = {"access_token": "same", "expires_at": time.time() + 3600}
    await save_token(db_app, row, user, token)
    spec = await db_app.app.state.catalog.spec(row, user)
    await save_token(db_app, row, user, token)
    await db_app.app.state.oauth.mark_reauth_spec(spec)
    payload = await db_app.app.state.credentials.load(row.id, user)
    assert not payload.get("oauth_state")
    current = await db_app.app.state.catalog.spec(row, user)
    assert current.credential_scope != spec.credential_scope


async def test_only_one_of_two_pending_callbacks_can_commit(db_app, monkeypatch):
    discovery(monkeypatch, cimd=False)
    row = await server(db_app)
    states = []
    for _ in range(2):
        response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
        assert response.status_code == 200
        states.append(parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0])
    calls = []

    async def exchange(auth, data):
        calls.append(data["code"])
        return {"access_token": data["code"], "expires_at": None}

    monkeypatch.setattr(db_app.app.state.oauth, "exchange", exchange)
    for state, code, expected in zip(states, ["winner", "stale"], [303, 409], strict=True):
        response = await db_app.web.get("/api/v1/oauth/callback", params={
            "state": state, "code": code, "iss": "https://auth.test"})
        assert response.status_code == expected, response.text
    assert calls == ["winner"]


@pytest.mark.parametrize("change", ["401", "revoke"])
async def test_refresh_cannot_overwrite_reauth_or_revoked_access(db_app, monkeypatch, change):
    row, user = await oauth_row(db_app)
    expired = {"access_token": "expired", "refresh_token": "refresh", "expires_at": 0}
    await save_token(db_app, row, user, expired)
    entered, release = asyncio.Event(), asyncio.Event()

    async def exchange(auth, data):
        entered.set()
        await release.wait()
        return {"access_token": "refreshed", "expires_at": None}

    oauth = db_app.app.state.oauth
    monkeypatch.setattr(oauth, "exchange", exchange)
    job = asyncio.create_task(oauth.credentials(row, user))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == "401":
            payload = await db_app.app.state.credentials.load(row.id, user)
            await oauth.mark_reauth(row, user, expired, "http_401", epoch=payload["oauth_epoch"])
        else:
            async with db_app.db.locked() as session:
                (await session.get(User, user)).disabled = True
        release.set()
        with pytest.raises(GatewayError):
            await job
        payload = await db_app.app.state.credentials.load(row.id, user)
        assert payload["oauth_token"] == expired
        if change == "401":
            assert payload["oauth_state"]["status"] == "needs_reauth"
    finally:
        release.set()
        await asyncio.gather(job, return_exceptions=True)


async def test_access_revoked_during_discovery_cannot_register_a_client(db_app, monkeypatch):
    calls = discovery(monkeypatch, cimd=False)
    from mcp_manager import oauth_discovery
    original = oauth_discovery.discover

    async def discover_then_revoke(resource, auth):
        context = await original(resource, auth)
        async with db_app.db.locked() as session:
            (await session.get(User, db_app.actor["id"])).disabled = True
        return context

    monkeypatch.setattr(oauth_discovery, "discover", discover_then_revoke)
    row = await server(db_app)
    response = await db_app.web.post(f"/api/v1/mcps/{row.id}/oauth/start")
    assert response.status_code in {401, 403}
    assert calls == []
