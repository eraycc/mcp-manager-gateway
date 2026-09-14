"""Provider scope must never replace gateway credential ownership."""
from contextlib import asynccontextmanager

import pytest

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import set_setting
from mcp_manager.runtime import GatewayError
from mcp_manager.transports import auth_headers, validate_config


@pytest.mark.parametrize("owner_scope", ["user"])
async def test_provider_scope_preserves_isolation_cache_and_bearer(tmp_path, owner_scope):
    app = create_app(Settings(data_dir=tmp_path, secret_key="scope-regression"))
    async with app.router.lifespan_context(app):
        catalog = app.state.catalog
        row = await catalog.create({"name": "OAuth", "transport": "streamable-http",
            "config": {"url": "https://mcp.test/mcp", "auth": {
                "type": "oauth", "scope": owner_scope, "scopes": ["mcp:read"],
                "authorization_url": "https://auth.test/authorize",
                "token_url": "https://auth.test/token"}}})
        value = {"access_token": "test-access", "scope": "mcp:read", "expires_at": 9999999999}
        await set_setting(app.state.db, app.state.oauth.key(row, "alice"), catalog.seal(value))


        class Connection:
            async def discover(self):
                return {"tools": [{"name": "read", "inputSchema": {"type": "object"}}],
                        "resources": [], "prompts": [], "templates": []}

            async def call(self, name, arguments):
                return {"content": [{"type": "text", "text": "ok"}], "isError": False}

        @asynccontextmanager
        async def connect(spec):
            validate_config(spec.transport, spec.config)
            assert auth_headers(spec.config)["Authorization"] == "Bearer test-access"
            yield Connection()

        app.state.runtime.connector = connect
        row = await catalog.update(row.id, {"mode": "lazy"}, user_id="alice")
        spec = await catalog.spec(row, "alice")
        assert spec.config["auth"]["scope"] == owner_scope
        assert spec.config["auth"]["scopes"] == ["mcp:read"]
        assert spec.config["auth"]["granted_scope"] == "mcp:read"
        assert spec.credential_owner == "alice"
        assert spec.isolation == "user"
        result = await catalog.refresh(row.id, "alice")
        assert result["cache_status"] == "ready"
        assert catalog.cached(row, "alice")["tools"][0]["name"] == "read"
        lease = app.state.runtime.create_lease("alice", "test")
        try:
            assert not (await app.state.runtime.call(spec, lease.id, "read", {}))["isError"]
        finally:
            await app.state.runtime.release(lease.id)
        assert catalog.cached(row, "bob")["tools"] == []
        with pytest.raises(GatewayError, match="OAuth"):
            await catalog.spec(row, "bob")
        assert catalog.unseal(row.config)["auth"]["scope"] == owner_scope
