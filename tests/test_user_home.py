from pathlib import Path

from mcp_manager.config import Settings


def test_default_home_ignores_launch_directory_and_loads_user_dotenv(tmp_path, monkeypatch):
    home = tmp_path / "user-home"
    home.mkdir()
    (home / ".env").write_text("PORT=9123\nDATABASE_URL=sqlite://personal.sqlite\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("PORT=9999\n", encoding="utf-8")
    monkeypatch.setenv("MCP_MANAGER_HOME", str(home))
    monkeypatch.chdir(elsewhere)
    settings = Settings(secret_key="test-key")
    assert settings.data_dir == home
    assert settings.port == 9123
    assert settings.database_url == "sqlite+aiosqlite:///" + (home / "personal.sqlite").as_posix()
    assert not (elsewhere / "mcp-manager.sqlite").exists()


def test_environment_overrides_user_dotenv_and_data_dir_is_home_relative(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("PORT=9123\nDATA_DIR=storage\n", encoding="utf-8")
    monkeypatch.setenv("MCP_MANAGER_HOME", str(tmp_path))
    monkeypatch.setenv("PORT", "9456")
    settings = Settings(secret_key="test-key")
    assert settings.port == 9456
    assert settings.data_dir == tmp_path / "storage"


def test_default_home_is_dot_mcp_manager(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_MANAGER_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings = Settings(secret_key="test-key")
    assert settings.data_dir == tmp_path / ".mcp-manager"
