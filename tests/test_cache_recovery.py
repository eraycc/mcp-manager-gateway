"""Windows cache replacement failures and explicit disabled-service recovery."""
from mcp_manager.jsonl_store import JsonlStore
from urllib.parse import parse_qs, urlsplit

import pytest

from mcp_manager.runtime import GatewayError
from test_bug1_catalog import console
from test_discovery_failure_policy import Upstream


def deny_cache_replace(monkeypatch, *, transient=False):
    original = JsonlStore._append
    attempts = []
    def replace(store, raw):
        if store.path.parent.name == "cache":
            attempts.append(store.path)
            if not transient or len(attempts) <= 2:
                error = PermissionError(13, "cache file locked")
                error.winerror = 5
                raise error
        return original(store, raw)
    monkeypatch.setattr(JsonlStore, "_append", replace)
    return attempts


async def test_transient_windows_cache_lock_retries_without_disabling(tmp_path, monkeypatch):
    async with console(tmp_path) as (app, web, actor):
        app.state.runtime.connector = Upstream().connect
        attempts = deny_cache_replace(monkeypatch, transient=True)
        response = await web.post("/api/v1/mcps", json={"name": "healthy", "config": {"command": "fake"}})
        row = response.json()
        assert row["mode"] == "lazy"
        assert row["cache_status"] == "ready"
        assert row["tool_count"] == 1
        assert len(attempts) >= 3
        assert not list(app.state.catalog.cache_dir.glob("*.tmp"))


async def test_permanent_cache_failure_disables_and_does_not_mask_reason(tmp_path, monkeypatch):
    async with console(tmp_path) as (app, web, actor):
        app.state.runtime.connector = Upstream().connect
        row = await app.state.catalog.create({"name": "healthy", "config": {"command": "fake"}})
        with monkeypatch.context() as patch:
            deny_cache_replace(patch)
            with pytest.raises(GatewayError) as exc:
                await app.state.catalog.refresh(row.id)
            assert exc.value.code == "cache_write_failed"
            current = await app.state.catalog.get(row.id)
            assert current.mode == "disabled"
            cache = app.state.catalog.cached(current)
            assert cache["tools"] == []
            assert cache["cache_error_code"] == "cache_write_failed"
            assert "cache file locked" in cache["cache_error"]
            assert not list(app.state.catalog.cache_dir.glob("*.tmp"))
        recovered = await app.state.catalog.update(row.id, {"mode": "lazy"})
        assert app.state.catalog.cached(recovered)["cache_status"] == "ready"


@pytest.mark.parametrize("action", ["refresh", "start"])
async def test_explicit_admin_retry_recovers_auto_disabled_service(tmp_path, action):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        peer.failure = OSError("offline before retry")
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({"name": "service", "mode": "eager", "config": {"command": "fake"}})
        assert row.mode == "disabled"
        peer.failure = None
        response = await web.post("/api/v1/mcps/" + row.id + "/" + action)
        assert response.status_code == 200, response.text
        current = await app.state.catalog.get(row.id)
        assert current.mode == "eager"
        assert app.state.catalog.cached(current)["cache_status"] == "ready"
        assert len(app.state.catalog.cached(current)["tools"]) == 1


@pytest.mark.parametrize("action", ["refresh", "start"])
async def test_explicit_retry_failure_preserves_latest_reason(tmp_path, action):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        peer.failure = OSError("old failure")
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({"name": "service", "config": {"command": "fake"}})
        peer.failure = OSError("new upstream failure")
        response = await web.post("/api/v1/mcps/" + row.id + "/" + action)
        assert response.status_code >= 400
        current = await app.state.catalog.get(row.id)
        assert current.mode == "disabled"
        assert "new upstream failure" in app.state.catalog.cached(current)["cache_error"]
        assert app.state.catalog.cached(current)["tools"] == []


async def test_manual_disable_is_not_undone_by_refresh(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({"name": "service", "mode": "disabled", "config": {"command": "fake"}})
        response = await web.post("/api/v1/mcps/" + row.id + "/refresh")
        assert response.status_code >= 400
        assert (await app.state.catalog.get(row.id)).mode == "disabled"
        assert peer.starts == 0
        # Start is an explicit enable action, unlike automatic refresh/agent access.
        response = await web.post("/api/v1/mcps/" + row.id + "/start")
        assert response.status_code == 200
        assert (await app.state.catalog.get(row.id)).mode == "lazy"


async def test_oauth_authorization_then_start_uses_current_users_credentials(tmp_path, monkeypatch):
    async with console(tmp_path) as (app, web, actor):
        app.state.runtime.connector = Upstream().connect
        row = await app.state.catalog.create({"name": "personal", "transport": "streamable-http",
            "config": {"url": "https://mcp.test", "auth": {"type": "oauth", "scope": "user",
            "authorization_url": "https://auth.test/authorize", "token_url": "https://auth.test/token"}}},
            user_id=actor["id"])
        assert row.mode == "lazy"
        start = await web.post("/api/v1/mcps/" + row.id + "/oauth/start")
        params = parse_qs(urlsplit(start.json()["authorization_url"]).query)
        async def exchange(auth, data):
            return {"access_token": "current-owner"}
        monkeypatch.setattr(app.state.oauth, "exchange", exchange)
        assert (await web.get("/api/v1/oauth/callback", params={"state": params["state"][0], "code": "demo"})).status_code == 303
        assert (await app.state.catalog.get(row.id)).mode == "lazy"
        assert (await web.post("/api/v1/mcps/" + row.id + "/start")).status_code == 200
        current = await app.state.catalog.get(row.id)
        assert app.state.catalog.cached(current, actor["id"])["cache_status"] == "ready"
        assert app.state.catalog.cached(current, "other-user")["tools"] == []
        with pytest.raises(GatewayError, match="authorization"):
            await app.state.oauth.credentials(current, "other-user")


async def test_refreshing_hides_the_correct_personal_cache(tmp_path):
    from mcp_manager.database import set_setting
    async with console(tmp_path) as (app, web, actor):
        app.state.runtime.connector = Upstream().connect
        row = await app.state.catalog.create({"name": "personal", "mode": "disabled", "transport": "streamable-http",
            "config": {"url": "https://mcp.test", "auth": {"type": "oauth", "scope": "user",
            "authorization_url": "https://auth.test/authorize", "token_url": "https://auth.test/token"}}})
        await set_setting(app.state.db, app.state.oauth.key(row, actor["id"]), app.state.catalog.seal({"access_token":"valid"}))
        row = await app.state.catalog.update(row.id, {"mode":"lazy"}, user_id=actor["id"])
        assert app.state.catalog.cached(row, actor["id"])["tools"]
        app.state.catalog.mark_refreshing(row, actor["id"])
        assert app.state.catalog.cached(row, actor["id"])["tools"] == []
