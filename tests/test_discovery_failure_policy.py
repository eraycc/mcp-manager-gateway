"""Failed discovery must remove a service from the usable gateway directory."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from mcp_manager.runtime import GatewayError
from test_bug1_catalog import Connection, console


class Upstream:
    def __init__(self):
        self.failure = None
        self.discovery_failure = None
        self.starts = 0

    @asynccontextmanager
    async def connect(self, spec):
        self.starts += 1
        if self.failure:
            raise self.failure
        peer = Connection()
        original = peer.discover

        async def discover():
            if self.discovery_failure:
                raise self.discovery_failure
            return await original()

        peer.discover = discover
        yield peer


@pytest.mark.parametrize("action", ["refresh", "start", "edit", "enable", "scheduled"])
async def test_failed_discovery_disables_service_and_hides_old_tools(tmp_path, action):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = (await web.post("/api/v1/mcps", json={"name": "service", "config": {"command": "fake"}})).json()
        assert row["tool_count"] == 1
        old = await app.state.catalog.get(row["id"])
        peer.failure = OSError("upstream unavailable")
        if action in {"refresh", "start"}:
            result = await web.post("/api/v1/mcps/" + row["id"] + "/" + action)
            assert result.status_code >= 400
        elif action in {"edit", "enable"}:
            payload = {"description": "changed"} if action == "edit" else {"mode": "eager"}
            result = await web.patch("/api/v1/mcps/" + row["id"], json=payload)
            assert result.status_code == 200  # Configuration is saved, discovery failure is explicit.
            assert result.json()["mode"] == "disabled"
            assert result.json()["cache_status"] == "error"
        else:
            with pytest.raises(GatewayError):
                await app.state.catalog.refresh_target({"server_id": row["id"], "user_id": None})
        current = (await web.get("/api/v1/mcps/" + row["id"])).json()
        assert current["mode"] == "disabled"
        assert current["cache_status"] == "error"
        assert "upstream unavailable" in current["cache_error"]
        assert current["tool_count"] == 0
        assert current["cache_attempt_at"]
        assert current["revision"] > old.revision
        assert app.state.catalog.cached(old)["tools"] == []
        assert not app.state.runtime.instances
        assert not app.state.runtime.leases
        assert await app.state.catalog.refresh_targets() == []
        # Existing token scopes must not expose stale tools.
        async def principal(request):
            return SimpleNamespace(id=actor["id"]), None, [row["id"]]
        app.state.gateway.principal = principal
        assert (await app.state.gateway.directory(None))[3] == []
        # Recovery requires an explicit enabled policy and a successful discovery.
        peer.failure = None
        result = await web.patch("/api/v1/mcps/" + row["id"], json={"mode": "lazy"})
        assert result.json()["mode"] == "lazy"
        assert result.json()["cache_status"] == "ready"
        assert result.json()["tool_count"] == 1


@pytest.mark.parametrize("failure", ["startup", "discovery"])
async def test_failed_create_returns_saved_disabled_configuration(tmp_path, failure):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        if failure == "startup":
            peer.failure = FileNotFoundError("missing executable")
        else:
            peer.discovery_failure = RuntimeError("tools/list failed")
        app.state.runtime.connector = peer.connect
        response = await web.post("/api/v1/mcps", json={"name": "broken", "config": {"command": "fake"}})
        assert response.status_code == 200
        saved = response.json()
        assert saved["mode"] == "disabled"
        assert saved["cache_status"] == "error"
        assert saved["tool_count"] == 0
        assert saved["cache_error"]
        assert len(await app.state.catalog.rows()) == 1


async def test_lazy_resource_start_failure_also_disables_service(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({"name": "service", "config": {"command": "fake"}})
        peer.failure = OSError("upstream disappeared")
        lease = app.state.runtime.create_lease("system", "resource")
        try:
            with pytest.raises(GatewayError):
                await app.state.runtime.perform(await app.state.catalog.spec(row), lease.id, "read_resource", "test://one")
            assert (await app.state.catalog.get(row.id)).mode == "disabled"
            assert app.state.catalog.cached(row)["tools"] == []
        finally:
            await app.state.runtime.release(lease.id)


async def test_late_failure_cannot_disable_new_revision(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({"name": "service", "config": {"command": "fake"}})
        entered, finish = asyncio.Event(), asyncio.Event()
        original = app.state.runtime.perform

        async def paused(spec, *args, **kwargs):
            if spec.revision == row.revision:
                entered.set()
                await finish.wait()
                raise GatewayError("connection_error", "obsolete failure")
            return await original(spec, *args, **kwargs)

        app.state.runtime.perform = paused
        pending = asyncio.create_task(app.state.catalog.refresh(row.id))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            fresh = await app.state.catalog.update(row.id, {"description": "new"})
            finish.set()
            with pytest.raises(GatewayError):
                await pending
            current = await app.state.catalog.get(row.id)
            assert current.revision == fresh.revision
            assert current.mode == "lazy"
            assert app.state.catalog.cached(current)["cache_status"] == "ready"
        finally:
            finish.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_live_connection_tools_list_failure_is_not_success(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({"name": "service", "mode": "eager", "config": {"command": "fake"}})
        peer.discovery_failure = RuntimeError("tools/list stopped responding")
        result = await web.post("/api/v1/mcps/" + row.id + "/refresh")
        assert result.status_code >= 400
        assert (await app.state.catalog.get(row.id)).mode == "disabled"
        assert app.state.catalog.cached(await app.state.catalog.get(row.id))["tools"] == []
        assert not app.state.runtime.instances


@pytest.mark.parametrize("action", ["enable", "lazy"])
async def test_batch_policy_change_reports_discovery_failure(tmp_path, action):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        peer.failure = OSError("offline")
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({"name": "service", "mode": "disabled", "config": {"command": "fake"}})
        response = await web.post("/api/v1/mcps/batch", json={"ids": [row.id], "action": action})
        job = response.json()
        async with asyncio.timeout(5):
            while app.state.jobs.items[job["id"]]["status"] in {"queued", "running"}:
                await asyncio.sleep(.01)
        result = app.state.jobs.items[job["id"]]
        assert result["status"] == "completed_with_errors"
        assert result["results"][0]["ok"] is False
        assert "offline" in result["results"][0]["error"]
        assert (await app.state.catalog.get(row.id)).mode == "disabled"


@pytest.mark.parametrize("transport", ["stdio", "streamable-http", "sse"])
async def test_real_unreachable_upstream_is_saved_disabled(tmp_path, transport):
    import socket
    async with console(tmp_path) as (app, web, actor):
        # A bound, non-listening local socket prevents accidental connection to another service.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            config = {"command": str(tmp_path / "no-such-mcp-executable")}
            if transport != "stdio":
                config = {"url": f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"}
            config.update(startup_timeout=2, call_timeout=2, stop_timeout=1)
            result = await web.post("/api/v1/mcps", json={"name": "offline", "transport": transport, "config": config})
        assert result.status_code == 200
        row = result.json()
        assert row["mode"] == "disabled"
        assert row["auto_disabled"] is True
        assert row["cache_status"] == "error"
        assert row["tool_count"] == 0
        assert row["cache_error_code"] in {"startup_failed", "startup_timeout"}
        assert not app.state.runtime.instances


async def test_failure_is_persisted_across_restart(tmp_path):
    from mcp_manager.app import create_app
    from mcp_manager.config import Settings
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        peer.failure = OSError("offline")
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({"name": "service", "config": {"command": "fake"}})
        server_id = row.id
    restarted = create_app(Settings(data_dir=tmp_path, secret_key="bug1"))
    async with restarted.router.lifespan_context(restarted):
        row = await restarted.state.catalog.get(server_id)
        assert row.mode == "disabled"
        cached = restarted.state.catalog.cached(row)
        assert cached["tools"] == []
        assert cached["cache_status"] == "error"
        assert cached["auto_disabled"] is True
        assert "offline" in cached["cache_error"]
        assert await restarted.state.catalog.refresh_targets() == []


async def test_expired_oauth_discovery_preserves_service_policy_and_reason(tmp_path):
    from mcp_manager.database import set_setting
    async with console(tmp_path) as (app, web, actor):
        row = await app.state.catalog.create({"name": "oauth", "mode": "disabled",
            "transport": "streamable-http", "config": {"url": "http://127.0.0.1:1/mcp",
            "auth": {"type": "oauth", "scope": "user", "authorization_url": "https://auth.test/authorize",
                     "token_url": "https://auth.test/token"}}})
        await set_setting(app.state.db, app.state.oauth.key(row, actor["id"]), app.state.catalog.seal(
            {"access_token": "expired", "expires_at": 0}))
        response = await web.patch("/api/v1/mcps/" + row.id, json={"mode": "eager"})
        saved = response.json()
        assert saved["mode"] == "eager"
        assert saved["cache_error_code"] == "auth_required"
        assert "expired" in saved["cache_error"]
        assert saved["tool_count"] == 0
        # The administrator must still be able to repair OAuth while disabled.
        assert (await web.get("/api/v1/mcps/" + row.id + "/oauth/status")).status_code == 200
        assert (await web.post("/api/v1/mcps/" + row.id + "/oauth/start")).status_code == 200


async def test_real_offline_rest_create_and_refresh_disable(tmp_path):
    import socket
    async with console(tmp_path) as (app, web, actor):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            config = {"tools": [{"name": "add", "request": {"method": "POST",
                       "url": f"http://127.0.0.1:{sock.getsockname()[1]}/add"}}],
                      "startup_timeout": 2, "call_timeout": 2, "stop_timeout": 1}
            response = await web.post("/api/v1/mcps", json={"name": "REST", "transport": "rest",
                                      "mode": "eager", "config": config})
            row = response.json()
            assert row["mode"] == "disabled"
            assert row["cache_status"] == "error"
            assert row["tool_count"] == 0


@pytest.mark.parametrize("operation", ["refresh", "edit"])
async def test_unverified_directory_is_hidden_during_discovery(tmp_path, operation):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({"name": "service", "config": {"command": "fake"}})
        entered, finish = asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def slow_connect(spec):
            entered.set()
            await finish.wait()
            raise OSError("unreachable while validating")
            yield  # pragma: no cover
        app.state.runtime.connector = slow_connect
        operation_coro = (app.state.catalog.refresh(row.id) if operation == "refresh" else
                          app.state.catalog.update(row.id, {"mode": "eager"}))
        task = asyncio.create_task(operation_coro)
        try:
            await asyncio.wait_for(entered.wait(), 3)
            current = await app.state.catalog.get(row.id)
            assert app.state.catalog.cached(current)["tools"] == []
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)


async def test_agent_receives_startup_reason_and_service_is_disabled(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({"name": "service", "config": {"command": "fake"}})
        peer.failure = OSError("upstream connection refused")
        user = SimpleNamespace(id=actor["id"], username="admin")
        async def principal(request):
            return user, None, [row.id]
        app.state.gateway.principal = principal
        request = SimpleNamespace(headers={})
        result = await app.state.gateway.call_tool(SimpleNamespace(request=request),
            SimpleNamespace(name=row.slug + "__first", arguments={"value": 1}))
        payload = result.model_dump(by_alias=True)
        assert payload["isError"] is True
        assert payload["structuredContent"]["error"]["code"] == "startup_failed"
        assert "upstream connection refused" in payload["content"][0]["text"]
        assert (await app.state.catalog.get(row.id)).mode == "disabled"
        assert (await app.state.gateway.directory(request))[3] == []


async def test_nested_transport_failure_preserves_specific_reason(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        peer.failure = ExceptionGroup("transport tasks failed", [
            ExceptionGroup("reader failed", [ConnectionRefusedError("remote port refused connection")])])
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create({"name": "broken", "config": {"command": "fake"}})
        assert row.mode == "disabled"
        assert "remote port refused connection" in app.state.catalog.cached(row)["cache_error"]


async def test_personal_auth_failure_is_visible_only_to_its_owner(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        response = await web.post("/api/v1/mcps", json={"name": "personal", "transport": "streamable-http",
            "config": {"url": "https://mcp.test", "auth": {"type": "oauth", "scope": "user",
            "authorization_url": "https://auth.test/authorize", "token_url": "https://auth.test/token"}}})
        row = await app.state.catalog.get(response.json()["id"])
        assert row.mode == "lazy"
        owner_cache = app.state.catalog.cached(row, actor["id"])
        assert owner_cache["cache_status"] == "auth_required"
        assert owner_cache["cache_error_code"] == "auth_required"
        for observer in ("another-admin", None):
            cache = app.state.catalog.cached(row, observer)
            assert cache["tools"] == []
            assert cache["cache_status"] == "empty"
            assert cache["cache_error_code"] is None
