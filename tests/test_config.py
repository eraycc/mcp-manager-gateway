from mcp_manager.config import Settings, normalize_database_url


def test_database_urls_and_persistent_secret(tmp_path):
    assert normalize_database_url("mysql://u:p@localhost/db", tmp_path).startswith("mysql+asyncmy://")
    assert normalize_database_url("sqlite:///C:/data/main.db", tmp_path) == "sqlite+aiosqlite:///C:/data/main.db"
    assert normalize_database_url("sqlite:///relative.db", tmp_path).endswith("/relative.db")
    a=Settings(data_dir=tmp_path)
    b=Settings(data_dir=tmp_path)
    assert a.secret_key == b.secret_key
    assert a.jwt_secret
    assert a.data_dir.is_absolute()


def test_default_path_relative_data_and_shorthand(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_MANAGER_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    cfg=Settings(data_dir="data", secret_key="test-key", database_url="")
    assert cfg.data_dir == tmp_path/"data"
    assert cfg.database_url.endswith("/data/mcp-manager.sqlite")
    assert normalize_database_url("sqlite://relative.sqlite", tmp_path) == "sqlite+aiosqlite:///"+(tmp_path/"relative.sqlite").as_posix()
