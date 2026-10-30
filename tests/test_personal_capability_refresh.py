"""Personal capability refresh is explicit, credential-gated, and owner-scoped."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from starlette.requests import Request
from test_personal_credentials_api import credential_console

from mcp_manager.catalog_models import CatalogEntry, CatalogGeneration, CatalogHead
from mcp_manager.database import McpServer, User
from mcp_manager.runtime import GatewayError

AUTH = {
    "bearer": {"type": "bearer", "token": "personal-token"},
    "basic": {"type": "basic", "username": "personal", "password": "personal-password"},
    "api_key": {"type": "api_key", "header": "X-Personal-Key", "value": "personal-key"},
    "oauth": {"type": "oauth", "authorization_url": "https://auth.test/authorize",
              "token_url": "https://auth.test/token", "client_id": "client", "client_secret": "secret",
              "config_isolation": "user", "access_token": "personal-access"},
}


@asynccontextmanager
async def refresh_console(tmp_path, *, auth_type="bearer", transport="streamable-http", isolation="user", configured=True):
    async with credential_console(tmp_path) as (app, admin, actor):
        auth = dict(AUTH.get(auth_type, {"type": "none"}))
        if auth_type == "oauth_shared":
            auth = dict(AUTH["oauth"], config_isolation="shared")
        config = {"url": "https://mcp.test/mcp", "auth": auth}
        if transport == "stdio":
            config = {"command": "unused", "auth": auth}
        elif transport == "rest":
            config = {"tools": [{"name": "read", "inputSchema": {"type": "object"},
                                  "request": {"url": "https://mcp.test/read", "method": "GET"}}], "auth": auth}
        row = await app.state.catalog.create({
            "name": "Personal refresh", "transport": transport, "isolation": isolation,
            "mode": "lazy", "config": config,
        }, user_id=actor["id"], discover=False)
        response = await admin.post("/api/v1/users", json={
            "username": "reader", "password": "password12345", "role": "user",
            "scope_mode": "selected", "mcp_ids": [row.id],
        })
        assert response.status_code == 200, response.text
        reader = response.json()
        if configured and auth["type"] != "none":
            payload = {"auth_config": {key: value for key, value in auth.items()
                                       if key not in {"access_token", "config_isolation"}}}
            if auth["type"] == "oauth":
                payload["oauth_token"] = {"access_token": "reader-access"}
                if auth_type == "oauth_shared":
                    payload.pop("auth_config")
            async with app.state.db.locked() as session:
                await app.state.credentials.write(session, row.id, reader["id"], payload)
        control = {"connections": [], "fail": False, "during_discovery": None}

        class Connection:
            async def discover(self):
                if control["during_discovery"]:
                    await control["during_discovery"]()
                if control["fail"]:
                    raise GatewayError("discovery_failed", "Downstream unavailable")
                return {
                    "tools": [{"name": "read", "inputSchema": {"type": "object"}}],
                    "resources": [{"name": "Resource", "uri": "test://resource"}],
                    "prompts": [{"name": "Prompt", "arguments": []}],
                    "templates": [{"name": "Template", "uriTemplate": "test://{key}"}],
                }

            async def call(self, name, arguments):
                return {"content": [{"type": "text", "text": "ok"}]}

        @asynccontextmanager
        async def connect(spec):
            control["connections"].append(spec.credential_owner)
            yield Connection()
        app.state.runtime.connector = connect
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/api/v1/auth/login", json={"username": "reader", "password": "password12345"})
            assert response.status_code == 200
            client.headers["X-CSRF-Token"] = client.cookies["mcp_csrf"]
            yield app, admin, client, actor, reader, row, control


async def refresh(client, row, **extra):
    return await client.post(f"/api/v1/mcps/{row.id}/credentials/refresh",
                             json={"revision": row.revision, **extra})


@pytest.mark.parametrize("auth_type,transport", [
    ("bearer", "streamable-http"), ("basic", "streamable-http"), ("api_key", "streamable-http"),
    ("oauth", "streamable-http"), ("oauth_shared", "streamable-http"),
    ("bearer", "sse"), ("bearer", "rest"),
])
async def test_refresh_publishes_all_capability_kinds_for_only_current_user(tmp_path, auth_type, transport):
    async with refresh_console(tmp_path, auth_type=auth_type, transport=transport) as values:
        app, _admin, client, actor, reader, row, control = values
        await app.state.catalog.refresh_index(row.id, actor["id"])
        before = await app.state.catalog.index.get_summary(await app.state.catalog.index_scope(row, actor["id"]))
        credentials = await app.state.credentials.load(row.id, actor["id"])
        control["connections"].clear()
        status = (await client.get(f"/api/v1/mcps/{row.id}/credentials/status")).json()
        assert status["refresh_allowed"] is True
        response = await refresh(client, row)
        assert response.status_code == 200, response.text
        assert response.json()["cache_status"] == "ready"
        assert [response.json()[key] for key in ("tool_count", "resource_count", "prompt_count", "template_count")] == [1, 1, 1, 1]
        assert "inputSchema" not in response.text and "personal-token" not in response.text
        assert control["connections"] == [reader["id"]]
        after = await app.state.catalog.index.get_summary(await app.state.catalog.index_scope(row, actor["id"]))
        assert after == before
        assert await app.state.credentials.load(row.id, actor["id"]) == credentials


@pytest.mark.parametrize("case", ["missing", "oauth_pending", "disabled", "shared", "stdio", "none", "stale", "spoof_owner", "revoked"])
async def test_refresh_rejects_invalid_scope_before_connecting(tmp_path, case):
    async with refresh_console(
        tmp_path, auth_type="oauth" if case == "oauth_pending" else "none" if case == "none" else "bearer",
        transport="stdio" if case == "stdio" else "streamable-http",
        isolation="service" if case == "shared" else "user", configured=case != "missing",
    ) as values:
        app, _admin, client, _actor, reader, row, control = values
        if case in {"disabled", "revoked", "oauth_pending"}:
            async with app.state.db.locked() as session:
                if case == "disabled":
                    (await session.get(McpServer, row.id)).mode = "disabled"
                elif case == "revoked":
                    (await session.get(User, reader["id"])).mcp_ids = []
                else:
                    payload = await app.state.credentials.load(row.id, reader["id"], session=session)
                    payload.pop("oauth_token")
                    await app.state.credentials.write(session, row.id, reader["id"], payload)
        extra = {"revision": row.revision + 1} if case == "stale" else {"user_id": "another-user"} if case == "spoof_owner" else {}
        response = await refresh(client, row, **extra)
        assert response.status_code >= 400, response.text
        assert control["connections"] == []
        current = await app.state.catalog.get(row.id)
        assert current.mode == ("disabled" if case == "disabled" else "lazy")


async def test_first_refresh_failure_retains_own_catalog_and_remains_retryable(tmp_path):
    async with refresh_console(tmp_path) as values:
        app, _admin, client, actor, reader, row, control = values
        await app.state.catalog.refresh_index(row.id, actor["id"])
        assert (await refresh(client, row)).status_code == 200
        control["fail"] = True
        response = await refresh(client, row)
        assert response.status_code >= 400
        own = (await client.get(f"/api/v1/mcps/{row.id}/capabilities?kind=tools")).json()
        assert [item["name"] for item in own["items"]] == ["read"]
        assert app.state.catalog.cached(row, reader["id"])["startup_failure_count"] == 1
        status = (await client.get(f"/api/v1/mcps/{row.id}/credentials/status")).json()
        assert status["refresh_allowed"] is True
        own_scope = await app.state.catalog.index_scope(row, reader["id"])
        own_summary = (await app.state.catalog.index.get_summaries([own_scope]))[row.id]
        assert own_summary["cache_status"] == "ready" and own_summary["tool_count"] == 1
        assert own_summary["cache_stale"]
        assert "Downstream unavailable" in own_summary["cache_error"]
        other = await app.state.catalog.index.get_summary(await app.state.catalog.index_scope(row, actor["id"]))
        assert other["status"] == "ready" and other["tool_count"] == 1
        control["fail"] = False
        assert (await refresh(client, row)).status_code == 200


@pytest.mark.parametrize("change", ["disable", "delete", "revoke"])
async def test_refresh_cannot_publish_after_administrator_changes_access(tmp_path, change):
    async with refresh_console(tmp_path) as values:
        app, _admin, client, _actor, reader, row, control = values

        async def mutate():
            control["during_discovery"] = None
            async with app.state.db.locked() as session:
                server = await session.get(McpServer, row.id)
                if change == "delete":
                    await session.delete(server)
                elif change == "disable":
                    server.mode = "disabled"
                    server.revision += 1
                else:
                    (await session.get(User, reader["id"])).mcp_ids = []
        control["during_discovery"] = mutate
        response = await asyncio.wait_for(refresh(client, row), 10)
        assert response.status_code >= 400, response.text
        async with app.state.db.session() as session:
            heads = (await session.scalars(select(CatalogHead).where(CatalogHead.server_id == row.id))).all()
            assert all(head.status != "ready" for head in heads)


async def test_disable_preserves_credentials_and_delete_removes_every_users_catalog(tmp_path):
    async with refresh_console(tmp_path) as values:
        app, _admin, client, actor, reader, row, _control = values
        await app.state.catalog.refresh_index(row.id, actor["id"])
        assert (await refresh(client, row)).status_code == 200
        credentials = {who: await app.state.credentials.load(row.id, who) for who in (actor["id"], reader["id"])}
        await app.state.catalog.update(row.id, {"mode": "disabled"}, user_id=actor["id"])
        async with app.state.db.session() as session:
            assert await session.scalar(select(func.count()).select_from(CatalogHead).where(CatalogHead.server_id == row.id)) == 2
        for who, value in credentials.items():
            assert await app.state.credentials.load(row.id, who) == value
        await app.state.catalog.delete(row.id)
        assert await app.state.credentials.owners(row.id) == set()
        async with app.state.db.session() as session:
            heads = (await session.scalars(select(CatalogHead))).all()
            assert all(head.generation_id is None and head.status != "ready" for head in heads)
        # Physical index retirement is bounded; it cannot cascade through every
        # posting while the administrator's delete request holds the writer lock.
        from mcp_manager.database import now
        from mcp_manager.retention import run_storage_retention
        for _ in range(60):
            await run_storage_retention(app.state, at=now())
        async with app.state.db.session() as session:
            for model in (CatalogGeneration, CatalogEntry):
                assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("outcome", ["renewed", "expired", "renewal_failed"])
async def test_oauth_refresh_handles_token_renewal_before_publishing(tmp_path, outcome):
    async with refresh_console(tmp_path, auth_type="oauth") as values:
        app, _admin, client, _actor, reader, row, control = values
        async with app.state.db.locked() as session:
            payload = await app.state.credentials.load(row.id, reader["id"], session=session)
            payload["oauth_token"]["expires_at"] = 0
            if outcome != "expired":
                payload["oauth_token"]["refresh_token"] = "reader-refresh"
            await app.state.credentials.write(session, row.id, reader["id"], payload)
        exchanges = []

        async def exchange(_auth, body):
            exchanges.append(body)
            if outcome == "renewal_failed":
                raise GatewayError("auth_required", "OAuth renewal failed")
            return {"access_token": "renewed-access", "expires_at": 4_000_000_000}
        app.state.oauth.exchange = exchange
        status = (await client.get(f"/api/v1/mcps/{row.id}/credentials/status")).json()
        assert status["refresh_allowed"] is (outcome != "expired")
        response = await refresh(client, row)
        if outcome == "renewed":
            assert response.status_code == 200, response.text
            assert control["connections"] == [reader["id"]]
            assert len(exchanges) == 1
        else:
            assert response.status_code >= 400
            assert control["connections"] == []
            if outcome == "renewal_failed":
                scope = await app.state.catalog.index_scope(row, reader["id"])
                summary = (await app.state.catalog.index.get_summaries([scope]))[row.id]
                assert summary["cache_status"] == "error"
                assert summary["cache_error_code"] == "auth_required"
                assert "重新授权" in summary["cache_error"]
                assert "OAuth renewal failed" not in summary["cache_error"]


async def test_agent_start_failures_and_scheduled_recovery_are_user_scoped(tmp_path):
    async with refresh_console(tmp_path) as values:
        app, admin, client, actor, reader, row, control = values
        catalog, runtime = app.state.catalog, app.state.runtime
        await catalog.refresh_index(row.id, actor["id"])
        await catalog.refresh_index(row.id, reader["id"])
        original_connector = runtime.connector
        failing = True

        @asynccontextmanager
        async def connect(spec):
            if failing and spec.credential_owner == reader["id"]:
                raise OSError("Personal downstream unavailable")
            async with original_connector(spec) as connection:
                yield connection

        runtime.connector = connect

        async def context(http):
            response = await http.post("/api/v1/tokens", json={
                "name": "failure recovery", "scope_mode": "all", "discovery_mode": "discovery",
            })
            assert response.status_code == 200, response.text
            return SimpleNamespace(request=Request({
                "type": "http", "method": "POST", "path": "/mcp", "app": app,
                "headers": [(b"authorization", ("Bearer " + response.json()["token"]).encode())],
            }))

        reader_context, admin_context = await context(client), await context(admin)

        async def invoke(ctx, name="gateway_call", **arguments):
            if name == "gateway_call":
                arguments = {"name": row.slug + "__read", "arguments": {}}
            return await app.state.gateway.call_tool(ctx, SimpleNamespace(name=name, arguments=arguments))

        for count in (1, 2, 3):
            result = await invoke(reader_context)
            assert result.is_error
            assert catalog.cached(row, reader["id"])["startup_failure_count"] == count
        failed = catalog.public(row, user_id=reader["id"])
        assert failed["status"] == "failed"
        capabilities = (await client.get(f"/api/v1/mcps/{row.id}/capabilities?kind=tools")).json()
        assert capabilities["items"] == []
        summary = await invoke(reader_context, "gateway_search_mcps", query="*")
        own = summary.structured_content["items"][0]
        assert own["tool_count"] == 0 and own["catalog_status"] == "error"
        assert own["failure_reason"]
        assert catalog.cached(row, actor["id"])["cache_status"] == "ready"
        assert not (await invoke(admin_context)).is_error

        failing = False
        targets = await catalog.refresh_targets(row.id)
        assert {target["user_id"] for target in targets} == {actor["id"], reader["id"]}
        for target in targets:
            await catalog.refresh_target(target)
        recovered = catalog.cached(row, reader["id"])
        assert recovered["cache_status"] == "ready"
        assert recovered["runtime_status"] != "failed"
        assert recovered["startup_failure_count"] == 0
        assert recovered["failed_gateway_names"] == []
        assert recovered["last_startup_error"] is None
        assert not (await invoke(reader_context)).is_error

        disabled = await catalog.update(row.id, {"revision": row.revision, "mode": "disabled"}, user_id=actor["id"])
        assert disabled.mode == "disabled"
        assert await catalog.refresh_targets(row.id) == []
        for http in (admin, client):
            available = (await http.get("/api/v1/mcps?enabled_only=true")).json()
            assert row.id not in {item["id"] for item in available["items"]}
        before = list(control["connections"])
        with pytest.raises(GatewayError):
            await catalog.refresh_target(targets[0])
        assert control["connections"] == before
