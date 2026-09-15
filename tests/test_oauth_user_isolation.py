"""OAuth identities and failure domains must follow the API token owner."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import types
from starlette.requests import Request
from test_bug1_catalog import console

from mcp_manager.database import McpServer, set_setting
from mcp_manager.runtime import GatewayError
from mcp_manager.transports import auth_headers, validate_config


def configuration(scope=None):
    auth = {"type": "oauth", "authorization_url": "https://auth.test/authorize",
            "token_url": "https://auth.test/token"}
    if scope is not None:
        auth["scope"] = scope
    return {"url": "https://mcp.test/mcp", "auth": auth}


@pytest.mark.parametrize("legacy_scope", [None, "service", "user"])
async def test_legacy_oauth_never_reuses_shared_credentials(tmp_path, legacy_scope):
    async with console(tmp_path) as (app, web, _actor):
        catalog = app.state.catalog
        row = await catalog.create({"name": "legacy", "transport": "streamable-http",
                                    "mode": "disabled", "config": configuration()})
        async with app.state.db.locked() as session:
            saved = await session.get(McpServer, row.id)
            saved.config = catalog.seal(configuration(legacy_scope))
            saved.mode = "lazy"
        row = await catalog.get(row.id)
        await set_setting(app.state.db, "oauth:" + row.id + ":service",
                          catalog.seal({"access_token": "shared-secret"}))
        assert catalog.unseal(row.config)["auth"]["scope"] == "user"
        assert catalog.cache_key(row, "alice") != catalog.cache_key(row, "bob")
        for user_id in (None, "alice", "bob"):
            with pytest.raises(GatewayError, match="OAuth"):
                await catalog.spec(row, user_id)
        assert (await web.get("/api/v1/mcps/" + row.id + "/oauth/status")).json() == {
            "authorized": False, "scope": "user"}


def test_new_shared_oauth_configuration_is_rejected():
    with pytest.raises(ValueError, match="user"):
        validate_config("streamable-http", configuration("service"))


@pytest.mark.parametrize("failure", ["disconnect", "expired", "discovery"])
async def test_tokens_use_own_oauth_and_one_user_failure_preserves_other(tmp_path, failure):
    async with console(tmp_path) as (app, web, alice):
        catalog = app.state.catalog
        bob = (await web.post("/api/v1/users", json={
            "username": "bob", "password": "password12345", "scope_mode": "all"})).json()
        row = await catalog.create({"name": "personal", "slug": "personal",
            "transport": "streamable-http", "config": configuration()})
        assert row.mode == "lazy"
        calls, broken = [], set()

        class Connection:
            def __init__(self, spec):
                self.spec = spec

            async def discover(self):
                if self.spec.credential_owner in broken:
                    raise OSError("one user's upstream discovery failed")
                return {"tools": [{"name": "whoami", "inputSchema": {"type": "object"}}],
                        "resources": [], "prompts": [], "templates": []}

            async def call(self, name, arguments):
                bearer = auth_headers(self.spec.config)["Authorization"]
                calls.append((self.spec.credential_owner, bearer))
                return {"content": [{"type": "text", "text": bearer}]}

        @asynccontextmanager
        async def connect(spec):
            yield Connection(spec)

        app.state.runtime.connector = connect
        contexts = {}
        for user in (alice, bob):
            await set_setting(app.state.db, app.state.oauth.key(row, user["id"]),
                catalog.seal({"access_token": "oauth-" + user["username"], "expires_at": 9999999999}))
            await catalog.refresh(row.id, user["id"])
            token = (await web.post("/api/v1/tokens", json={"name": user["username"],
                "user_id": user["id"], "discovery_mode": "discovery", "scope_mode": "all"})).json()
            contexts[user["id"]] = SimpleNamespace(request=Request({"type": "http", "method": "POST",
                "path": "/mcp", "app": app,
                "headers": [(b"authorization", ("Bearer " + token["token"]).encode())]}))

        async def invoke(user):
            return await app.state.gateway.call_tool(contexts[user["id"]],
                types.CallToolRequestParams(name="gateway_call",
                    arguments={"name": "personal__whoami", "arguments": {}}))

        for user in (alice, bob):
            result = await invoke(user)
            assert not result.is_error, result
            assert result.content[0].text == "Bearer oauth-" + user["username"]
        before = row.revision
        if failure == "disconnect":
            assert (await web.post("/api/v1/mcps/" + row.id + "/oauth/disconnect")).status_code == 200
        else:
            if failure == "expired":
                await set_setting(app.state.db, app.state.oauth.key(row, alice["id"]),
                                  catalog.seal({"access_token": "expired", "expires_at": 0}))
            else:
                broken.add(alice["id"])
            with pytest.raises((GatewayError, OSError)):
                await catalog.refresh(row.id, alice["id"])
        current = await catalog.get(row.id)
        assert (current.mode, current.revision) == ("lazy", before)
        alice_cache = catalog.cached(current, alice["id"])
        if failure == "disconnect":
            assert alice_cache["tools"] == []
        else:
            assert alice_cache["cache_status"] == "ready"
            assert [tool["name"] for tool in alice_cache["tools"]] == ["whoami"]
            assert alice_cache["last_refresh_error"]
            assert alice_cache["startup_failure_count"] == 0
        assert catalog.cached(current, bob["id"])["cache_status"] == "ready"
        assert not (await invoke(bob)).is_error
        assert calls[-1] == (bob["id"], "Bearer oauth-bob")
        alice_result = await invoke(alice)
        assert alice_result.is_error is (failure != "discovery")
        specs = [item.spec for item in app.state.runtime.instances.values()]
        assert all(spec.isolation == "user" for spec in specs)
