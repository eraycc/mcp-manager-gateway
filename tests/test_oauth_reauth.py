"""OAuth failures stop at a persisted reauthorization boundary without replay."""
import time

import pytest

from mcp_manager.oauth import OAuth
from mcp_manager.runtime import GatewayError, ServerSpec


async def oauth_row(h):
    user = h.actor["id"]
    row = await h.app.state.catalog.create({
        "name": "OAuth state", "transport": "streamable-http", "isolation": "user",
        "config": {"url": "https://mcp.test/mcp", "auth": {
            "type": "oauth", "auth_mode": "manual", "client_id": "public",
            "authorization_url": "https://auth.test/authorize", "token_url": "https://auth.test/token"}},
    }, user_id=user)
    return row, user


async def save_token(h, row, user, token):
    async with h.db.locked() as session:
        await h.app.state.credentials.save_oauth_token(session, row, user, token)


async def test_failed_refresh_is_persisted_and_not_retried_after_restart(db_app, monkeypatch):
    row, user = await oauth_row(db_app)
    await save_token(db_app, row, user, {"access_token": "old", "refresh_token": "refresh", "expires_at": 0})
    calls = []
    async def exchange(auth, data):
        calls.append(data["grant_type"])
        raise GatewayError("oauth_failed", "invalid_grant")
    oauth = db_app.app.state.oauth
    monkeypatch.setattr(oauth, "exchange", exchange)
    for _ in range(3):
        with pytest.raises(GatewayError) as failure:
            await oauth.credentials(row, user)
        assert failure.value.code == "auth_required"
    assert calls == ["refresh_token"]
    restarted = OAuth(db_app.app.state.catalog, db_app.app.state.credentials)
    monkeypatch.setattr(restarted, "exchange", exchange)
    with pytest.raises(GatewayError):
        await restarted.credentials(row, user)
    assert calls == ["refresh_token"]
    state = (await db_app.web.get(f"/api/v1/mcps/{row.id}/oauth/status")).json()
    assert state["auth_status"] == "needs_reauth" and state["authorized"] is False
    status = await db_app.app.state.credentials.status(row, user)
    assert status["state"] == "needs_reauth" and status["action"] == "authorize"


async def test_expired_without_refresh_marks_reauth_but_unknown_expiry_remains_usable(db_app):
    row, user = await oauth_row(db_app)
    await save_token(db_app, row, user, {"access_token": "unknown", "expires_at": None})
    assert (await db_app.app.state.oauth.credentials(row, user))["access_token"] == "unknown"
    await save_token(db_app, row, user, {"access_token": "expired", "expires_at": 0})
    with pytest.raises(GatewayError) as error:
        await db_app.app.state.oauth.credentials(row, user)
    assert error.value.code == "auth_required"
    payload = await db_app.app.state.credentials.load(row.id, user)
    assert payload["oauth_state"]["status"] == "needs_reauth"


async def test_delayed_401_does_not_invalidate_newly_authorized_token(db_app):
    row, user = await oauth_row(db_app)
    original = {"access_token": "old", "expires_at": time.time() + 3600}
    await save_token(db_app, row, user, original)
    spec = ServerSpec(row.id, row.transport, {"auth": {"type": "oauth", **original}},
                      isolation="user", revision=row.revision, credential_owner=user)
    await save_token(db_app, row, user, {"access_token": "new", "expires_at": time.time() + 3600})
    await db_app.app.state.oauth.mark_reauth_spec(spec)
    payload = await db_app.app.state.credentials.load(row.id, user)
    assert not payload.get("oauth_state")
    assert (await db_app.app.state.oauth.credentials(row, user))["access_token"] == "new"
