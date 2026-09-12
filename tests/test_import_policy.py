import httpx
import pytest

from mcp_manager.environment import connection_config
from mcp_manager.imports import fingerprint, normalize_import
from mcp_manager.runtime import GatewayError
from mcp_manager.transports import RestConnection, auth_headers


def test_dedup_retains_codex_tool_override_policy():
    one = {"transport": "stdio", "config": {"command": "node"}}
    two = {"transport": "stdio", "config": {"command": "node", "tools": {"delete": {"enabled": False}}}}
    assert fingerprint(one) != fingerprint(two)


@pytest.mark.parametrize(("channel", "url"), [("claude", "${TEST_MCP_URL}"), ("dsh", "$TEST_MCP_URL")])
def test_full_environment_url_can_be_previewed_before_host_env_is_set(channel, url):
    data = normalize_import({"mcpServers": {"demo": {"url": url}}}, channel)
    assert not data["errors"]
    assert data["items"][0]["config"]["url"] == url


def test_expanded_environment_values_are_literal_not_recursive(monkeypatch):
    monkeypatch.setenv("TOKEN", "$OTHER")
    monkeypatch.setenv("OTHER", "wrong-token")
    config = connection_config({"environment_expansion": "dsh", "headers": {"Authorization": "$TOKEN"}})
    assert auth_headers(config)["Authorization"] == "$OTHER"


async def test_rest_tool_policy_hides_and_blocks_disallowed_tools():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        connection = RestConnection({"tools": [{"name": "delete", "request": {"url": "https://example.com"}},
                                             {"name": "read", "request": {"url": "https://example.com"}}],
                                     "disabled_tools": ["delete"]}, client)
        assert [t["name"] for t in (await connection.discover())["tools"]] == ["read"]
        with pytest.raises(GatewayError, match="disabled"):
            await connection.call("delete", {})
