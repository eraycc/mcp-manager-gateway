"""MCP protocol identity and modern/legacy compatibility regressions."""
from importlib.metadata import PackageNotFoundError

from mcp import Client
from test_bug1_catalog import console

from mcp_manager import about, bridge


def test_project_identity_reads_pyproject(tmp_path):
    loader = getattr(about, "load_project_identity", None)
    assert loader is not None, "project identity loader is missing"
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "sample-gateway"\nversion = "9.8.7"\n',
        encoding="utf-8",
    )

    assert loader(pyproject) == ("sample-gateway", "9.8.7")


def test_project_identity_uses_safe_defaults_when_sources_fail(tmp_path, monkeypatch):
    loader = getattr(about, "load_project_identity", None)
    assert loader is not None, "project identity loader is missing"

    def unavailable(_name):
        raise PackageNotFoundError("missing")

    monkeypatch.setattr(about, "distribution", unavailable, raising=False)
    assert loader(tmp_path / "missing.toml") == ("mcp-manager-gateway", "1.0.0")


def test_project_identity_uses_safe_defaults_for_wrong_field_types(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = 17\nversion = []\n', encoding="utf-8")

    assert about.load_project_identity(pyproject) == ("mcp-manager-gateway", "1.0.0")


def test_project_identity_survives_path_metadata_failure(tmp_path, monkeypatch):
    def unavailable(*_args):
        raise OSError("metadata unavailable")

    monkeypatch.setattr(about.Path, "is_file", unavailable)
    monkeypatch.setattr(about, "distribution", unavailable)

    assert about.load_project_identity(tmp_path / "pyproject.toml") == (
        "mcp-manager-gateway",
        "1.0.0",
    )


async def test_gateway_identity_is_project_metadata_in_modern_and_legacy_modes(tmp_path):
    async with console(tmp_path) as (app, web, _actor):
        async with Client(app.state.protocol, cache=None) as modern:
            assert modern.protocol_version == "2026-07-28"
            assert modern.server_info.name == "mcp-manager-gateway"
            assert modern.server_info.version == "0.1.8"

        async with Client(app.state.protocol, mode="legacy", cache=None) as legacy:
            assert legacy.protocol_version == "2025-11-25"
            assert legacy.server_info.name == "mcp-manager-gateway"
            assert legacy.server_info.version == "0.1.8"

        script = await web.get("/app.js")
        assert script.status_code == 200
        assert "server/discover" in script.text
        assert "2026-07-28" in script.text
        assert "2025-03-26" not in script.text


async def test_stdio_bridge_advertises_the_same_project_identity():
    factory = getattr(bridge, "create_bridge_server", None)
    assert factory is not None, "bridge server factory is missing"

    class Upstream:
        async def request(self, *_args, **_kwargs):
            raise AssertionError("protocol discovery must not call an upstream capability")

    server = factory(Upstream())
    async with Client(server, cache=None) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_info.name == "mcp-manager-gateway"
        assert client.server_info.version == "0.1.8"
