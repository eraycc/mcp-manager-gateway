"""Credential, cache, and transport ownership follows McpServer.isolation."""
import pytest

from mcp_manager import transports
from mcp_manager.app import create_app
from mcp_manager.config import Settings


@pytest.mark.parametrize("transport", ["streamable-http", "sse", "rest"])
def test_network_transport_rejects_session_isolation(transport):
    validate = getattr(transports, "validate_service_config", None)
    assert callable(validate), "service-level isolation validation is missing"
    config = (
        {"tools": [{"name": "x", "request": {"url": "https://api.test/x"}}]}
        if transport == "rest"
        else {"url": "https://mcp.test/mcp"}
    )
    with pytest.raises(ValueError, match="session.*stdio"):
        validate(transport, "session", config)


@pytest.mark.parametrize("isolation", ["service", "user", "session"])
def test_stdio_accepts_every_isolation_mode(isolation):
    validate = getattr(transports, "validate_service_config", None)
    assert callable(validate), "service-level isolation validation is missing"
    validate("stdio", isolation, {"command": "server"})


def test_legacy_oauth_scope_does_not_control_local_ownership():
    transports.validate_config("streamable-http", {
        "url": "https://mcp.test/mcp",
        "auth": {
            "type": "oauth",
            "scope": "service",
            "config_isolation": "shared",
            "authorization_url": "https://auth.test/authorize",
            "token_url": "https://auth.test/token",
        },
    })


@pytest.mark.asyncio
async def test_user_isolation_gives_bearer_users_distinct_cache_keys(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="credential-scope"))
    async with app.router.lifespan_context(app):
        row = await app.state.catalog.create({
            "name": "personal bearer",
            "transport": "streamable-http",
            "mode": "disabled",
            "isolation": "user",
            "config": {
                "url": "https://mcp.test/mcp",
                "auth": {"type": "bearer", "token": "admin-token"},
            },
        }, user_id="alice")
        assert app.state.catalog.cache_key(row, "alice") != app.state.catalog.cache_key(row, "bob")


@pytest.mark.asyncio
async def test_service_and_session_isolation_share_catalog_owner(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="credential-scope-global"))
    async with app.router.lifespan_context(app):
        for isolation in ("service", "session"):
            row = await app.state.catalog.create({
                "name": "global " + isolation,
                "transport": "stdio",
                "mode": "disabled",
                "isolation": isolation,
                "config": {"command": "server"},
            })
            assert app.state.catalog.cache_key(row, "alice") == app.state.catalog.cache_key(row, "bob")
