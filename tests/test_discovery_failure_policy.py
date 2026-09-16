"""Failure policy regressions for policy/status separation and scoped JSONL state."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from mcp_manager.runtime import GatewayError
from test_bug1_catalog import Connection, console


class Upstream:
    def __init__(self):
        self.failure = None
        self.discovery_failure = None
        self.starts = 0
        self.discovery_calls = 0

    @asynccontextmanager
    async def connect(self, spec):
        self.starts += 1
        if self.failure:
            raise self.failure
        peer = Connection()
        original = peer.discover

        async def discover():
            self.discovery_calls += 1
            if self.discovery_failure:
                raise self.discovery_failure
            return await original()

        peer.discover = discover
        yield peer


@pytest.mark.parametrize("failure", ["startup", "discovery"])
async def test_failed_create_preserves_mode_and_records_first_failure(tmp_path, failure):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        if failure == "startup":
            peer.failure = FileNotFoundError("missing executable")
        else:
            peer.discovery_failure = RuntimeError("tools/list failed")
        app.state.runtime.connector = peer.connect
        response = await web.post(
            "/api/v1/mcps",
            json={"name": "broken", "config": {"command": "fake"}},
        )
        assert response.status_code == 200
        saved = response.json()
        assert saved["mode"] == "lazy"
        assert saved["status"] == "stopped"
        assert saved["startup_failure_count"] == 1
        assert saved["cache_status"] == "error"
        assert saved["tool_count"] == 0
        assert saved["last_startup_error"]
        assert saved["failure_scope"] == "global"


async def test_failure_is_persisted_across_restart(tmp_path):
    from mcp_manager.app import create_app
    from mcp_manager.config import Settings

    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        peer.failure = OSError("offline")
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create(
            {"name": "service", "config": {"command": "fake"}}
        )
        server_id = row.id
        assert app.state.catalog.cached(row)["startup_failure_count"] == 1

    restarted = create_app(Settings(data_dir=tmp_path, secret_key="bug1"))
    async with restarted.router.lifespan_context(restarted):
        row = await restarted.state.catalog.get(server_id)
        cache = restarted.state.catalog.cached(row)
        assert row.mode == "lazy"
        assert cache["startup_failure_count"] == 1
        assert "offline" in cache["last_startup_error"]


async def test_late_failure_cannot_change_new_revision_state(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create(
            {"name": "service", "config": {"command": "fake"}}
        )
        entered = asyncio.Event()
        finish = asyncio.Event()
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
            assert app.state.catalog.cached(current)["startup_failure_count"] == 0
        finally:
            finish.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_nested_transport_failure_preserves_specific_reason(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        peer.failure = ExceptionGroup(
            "transport tasks failed",
            [ExceptionGroup(
                "reader failed",
                [ConnectionRefusedError("remote port refused connection")],
            )],
        )
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create(
            {"name": "broken", "config": {"command": "fake"}}
        )
        cache = app.state.catalog.cached(row)
        assert row.mode == "lazy"
        assert cache["startup_failure_count"] == 1
        assert "remote port refused connection" in cache["last_startup_error"]


async def test_personal_auth_failure_is_visible_only_to_its_owner(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        response = await web.post(
            "/api/v1/mcps",
            json={
                "name": "personal",
                "transport": "streamable-http",
                "isolation": "user",
                "config": {
                    "url": "https://mcp.test",
                    "auth": {
                        "type": "oauth",
                        "scope": "user",
                        "authorization_url": "https://auth.test/authorize",
                        "token_url": "https://auth.test/token",
                    },
                },
            },
        )
        row = await app.state.catalog.get(response.json()["id"])
        owner_cache = app.state.catalog.cached(row, actor["id"])
        assert row.mode == "lazy"
        assert owner_cache["cache_status"] == "auth_required"
        assert owner_cache["cache_error_code"] == "auth_required"
        assert owner_cache["failure_scope"] == "user"
        for observer in ("another-admin", None):
            cache = app.state.catalog.cached(row, observer)
            assert cache["tools"] == []
            assert cache["cache_status"] == "empty"
            assert cache["cache_error_code"] is None
