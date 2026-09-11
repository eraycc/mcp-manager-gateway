import pytest
from mcp_manager.transports import auth_headers
from mcp_manager.runtime import GatewayError
from mcp_manager.environment import connection_config, stdio_environment
from mcp_manager.transports import SdkConnection


def test_windows_stdio_receives_system_install_paths_without_unrelated_secrets(monkeypatch):
    monkeypatch.setenv("ProgramFiles", "C:/Program Files")
    monkeypatch.setenv("ProgramFiles(x86)", "C:/Program Files (x86)")
    monkeypatch.setenv("PRIVATE_APP_SECRET", "not-forwarded")
    result = stdio_environment({"CUSTOM": "explicit"}, platform="win32")
    assert result["ProgramFiles"] == "C:/Program Files"
    assert result["CUSTOM"] == "explicit"
    assert "PRIVATE_APP_SECRET" not in result
    assert stdio_environment({"ProgramFiles": "D:/Apps"}, platform="win32")["ProgramFiles"] == "D:/Apps"
    assert "ProgramFiles" not in stdio_environment({}, platform="linux")


def test_codex_header_environment_is_resolved_at_connection_time(monkeypatch):
    config = {"headers": {"X-App": "demo"}, "env_headers": {"X-Key": "TEST_KEY"},
              "auth": {"type": "bearer", "token_env": "TEST_TOKEN"}}
    monkeypatch.setenv("TEST_KEY", "key-value")
    monkeypatch.setenv("TEST_TOKEN", "first")
    assert auth_headers(config) == {"X-App": "demo", "X-Key": "key-value", "Authorization": "Bearer first"}
    monkeypatch.setenv("TEST_TOKEN", "rotated")
    assert auth_headers(config)["Authorization"] == "Bearer rotated"
    assert config["auth"] == {"type": "bearer", "token_env": "TEST_TOKEN"}


def test_claude_and_dsh_expansion_preserves_original_config(monkeypatch):
    monkeypatch.setenv("TEST_HOME", "/opt/tools")
    original = {"environment_expansion": "claude", "command": "${TEST_HOME}/run",
                "args": ["${NOT_SET:-fallback}"], "env_vars": ["TEST_HOME"]}
    assert connection_config(original)["command"] == "/opt/tools/run"
    assert connection_config(original)["args"] == ["fallback"]
    assert connection_config(original)["env"]["TEST_HOME"] == "/opt/tools"
    assert original["command"] == "${TEST_HOME}/run"
    assert connection_config({"environment_expansion": "dsh", "env": {"DIR": "$TEST_HOME"}})["env"]["DIR"] == "/opt/tools"


async def test_imported_tool_deny_list_cannot_be_bypassed_by_calling_name():
    class FakeClient:
        async def call_tool(self, name, arguments):
            pytest.fail("Forbidden tool reached the downstream")
    connection = SdkConnection(FakeClient(), {"disabled_tools": ["delete"]})
    with pytest.raises(GatewayError, match="disabled"):
        await connection.call("delete", {})


def test_missing_environment_auth_fails_closed(monkeypatch):
    monkeypatch.delenv("UNSET_MCP_IMPORT_TOKEN", raising=False)
    with pytest.raises(GatewayError, match="UNSET_MCP_IMPORT_TOKEN"):
        auth_headers({"auth": {"type": "bearer", "token_env": "UNSET_MCP_IMPORT_TOKEN"}})
