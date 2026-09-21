import json
from types import SimpleNamespace

import pytest

from mcp_manager.runtime import GatewayError
from test_bug1_catalog import console
from test_discovery_failure_policy import Upstream


async def test_start_failures_keep_policy_until_threshold_then_mark_failed(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create(
            {"name": "service", "config": {"command": "fake"}}
        )
        old_tools = app.state.catalog.cached(row)["tools"]
        peer.failure = OSError("upstream unavailable")

        for expected_count in (1, 2):
            with pytest.raises(GatewayError) as caught:
                await app.state.catalog.refresh(row.id)
            current = await app.state.catalog.get(row.id)
            public = app.state.catalog.public(current, user_id=actor["id"])
            assert current.mode == "lazy"
            assert public["status"] == "stopped"
            assert public["startup_failure_count"] == expected_count
            assert app.state.catalog.cached(current)["tools"] == old_tools
            assert caught.value.details["startup_failure_count"] == expected_count

        with pytest.raises(GatewayError) as caught:
            await app.state.catalog.refresh(row.id)
        current = await app.state.catalog.get(row.id)
        public = app.state.catalog.public(current, user_id=actor["id"])
        assert current.mode == "lazy"
        assert public["status"] == "failed"
        assert public["startup_failure_count"] == 3
        assert app.state.catalog.cached(current)["tools"] == []
        assert caught.value.details["status"] == "failed"


async def test_successful_start_clears_failure_count_without_implicit_discovery(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create(
            {"name": "service", "config": {"command": "fake"}}
        )
        assert peer.discovery_calls == 1
        cached = app.state.catalog.cached(row)
        cached.update(
            startup_failure_count=1,
            last_startup_error_code="startup_failed",
            last_startup_error="old failure",
            last_startup_failure_at="2026-09-15T00:00:00+00:00",
        )
        app.state.catalog.save_cache(row, cached)
        lease = app.state.runtime.create_lease(actor["id"], "test")
        try:
            await app.state.catalog.call(
                row,
                lease,
                "first",
                {"value": 1},
                user=SimpleNamespace(id=actor["id"], username="admin"),
            )
        finally:
            await app.state.runtime.release(lease.id)
        assert peer.discovery_calls == 1
        assert app.state.catalog.public(
            row, user_id=actor["id"]
        )["startup_failure_count"] == 0


async def test_refresh_discovery_failure_preserves_ready_cache_and_status(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create(
            {"name": "service", "config": {"command": "fake"}}
        )
        peer.discovery_failure = RuntimeError("tools/list failed")
        with pytest.raises(GatewayError, match="tools/list failed"):
            await app.state.catalog.refresh(row.id)
        current = await app.state.catalog.get(row.id)
        cache = app.state.catalog.cached(current)
        public = app.state.catalog.public(current, user_id=actor["id"])
        assert cache["cache_status"] == "ready"
        assert [tool["name"] for tool in cache["tools"]] == ["first"]
        assert cache["last_refresh_error"] == "tools/list failed"
        assert public["status"] == "stopped"
        assert public["startup_failure_count"] == 0


async def test_first_discovery_failure_without_ready_cache_counts_as_start_failure(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        peer.discovery_failure = RuntimeError("first tools/list failed")
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create(
            {"name": "service", "mode": "disabled", "config": {"command": "fake"}}
        )
        current = await app.state.catalog.update(
            row.id,
            {"mode": "lazy", "revision": row.revision},
            user_id=actor["id"],
        )
        public = app.state.catalog.public(current, user_id=actor["id"])
        assert current.mode == "lazy"
        assert public["startup_failure_count"] == 1
        assert public["status"] == "stopped"
        assert app.state.catalog.cached(current)["tools"] == []


async def test_ready_empty_cache_is_preserved_on_refresh_failure(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        row = await app.state.catalog.create(
            {"name": "service", "config": {"command": "fake"}}
        )
        cached = app.state.catalog.cached(row)
        cached.update(tools=[], cache_status="ready")
        app.state.catalog.save_cache(row, cached)
        peer.discovery_failure = RuntimeError("empty tools/list failed")
        with pytest.raises(GatewayError, match="empty tools/list failed"):
            await app.state.catalog.refresh(row.id)
        cache = app.state.catalog.cached(await app.state.catalog.get(row.id))
        assert cache["cache_status"] == "ready"
        assert cache["tools"] == []
        assert cache["last_refresh_error"] == "empty tools/list failed"
        assert app.state.catalog.public(
            row, user_id=actor["id"]
        )["startup_failure_count"] == 0


async def test_status_filter_and_failed_agent_summary(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        peer = Upstream()
        app.state.runtime.connector = peer.connect
        failed = await app.state.catalog.create(
            {"name": "failed service", "slug": "failed", "config": {"command": "fake"}}
        )
        disabled = await app.state.catalog.create(
            {"name": "disabled service", "slug": "disabled", "mode": "disabled",
             "config": {"command": "fake"}}
        )
        cache = app.state.catalog.cached(failed)
        cache.update(
            runtime_status="failed",
            startup_failure_count=3,
            last_startup_error_code="startup_failed",
            last_startup_error="connection refused",
            last_startup_failure_at="2026-09-15T00:00:00+00:00",
            failure_scope="global",
            failed_gateway_names=["failed__first"],
            tools=[],
            cache_status="error",
            cache_at=None,
        )
        app.state.catalog.save_cache(failed, cache)

        listing = (await web.get("/api/v1/mcps", params={"status": "failed"})).json()
        assert [item["id"] for item in listing["items"]] == [failed.id]
        assert listing["items"][0]["mode"] == "lazy"

        user = SimpleNamespace(id=actor["id"], username="admin")
        token = SimpleNamespace(id="token", discovery_mode="discovery",
                                enable_resource_tools=False, enable_mcp_proposal=False)

        async def principal(request):
            return user, token, [failed.id, disabled.id]

        app.state.gateway.principal = principal
        request = SimpleNamespace(headers={})
        search = await app.state.gateway.call_tool(
            SimpleNamespace(request=request),
            SimpleNamespace(name="gateway_search_mcps", arguments={"query": "*"}),
        )
        body = json.loads(search.content[0].text)
        assert [item["id"] for item in body["items"]] == [failed.id]
        summary = body["items"][0]
        assert summary["status"] == "failed"
        assert summary["startup_failure_count"] == 3
        assert summary["failure_reason"] == "connection refused"
        assert summary["failure_scope"] == "global"
        assert summary["tools_list"] == []

        known = await app.state.gateway.call_tool(
            SimpleNamespace(request=request),
            SimpleNamespace(
                name="gateway_call",
                arguments={"name": "failed__first", "arguments": {"value": 1}},
            ),
        )
        assert known.structured_content["error"]["code"] == "mcp_failed"
        assert known.structured_content["error"]["mcp_id"] == failed.id

        unknown = await app.state.gateway.call_tool(
            SimpleNamespace(request=request),
            SimpleNamespace(
                name="gateway_call",
                arguments={"name": "failed__unknown", "arguments": {}},
            ),
        )
        assert unknown.structured_content["error"]["code"] != "mcp_failed"


async def test_personal_start_failure_counter_is_user_scoped(tmp_path):
    async with console(tmp_path) as (app, web, actor):
        row = await app.state.catalog.create(
            {
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
            user_id=actor["id"],
        )
        await app.state.catalog.record_start_failure(
            row,
            GatewayError("startup_failed", "owner credential rejected"),
            actor["id"],
        )
        owner = app.state.catalog.cached(row, actor["id"])
        observer = app.state.catalog.cached(row, "other-user")
        assert owner["failure_scope"] == "user"
        assert owner["startup_failure_count"] == 1
        assert observer["startup_failure_count"] == 0
        assert observer["last_startup_error"] is None
