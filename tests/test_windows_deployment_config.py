"""Windows launcher uses standard settings and exact owned-instance state."""
from pathlib import Path

from mcp_manager.config import Settings

ROOT = Path(__file__).parents[1]
MANAGE = ROOT / "deploy" / "windows" / "manage.ps1"
DAEMON = ROOT / "deploy" / "windows" / "mmg-daemon.ps1"


def test_daemon_passes_standard_config_and_records_owned_instance(monkeypatch, tmp_path):
    daemon = DAEMON.read_text(encoding="utf-8-sig")
    for name in ("HOST", "PORT", "PUBLIC_URL", "DATA_DIR", "DATABASE_URL", "COOKIE_SECURE"):
        assert f"$env:{name}" in daemon
    assert "$env:MCP_MANAGER_DATABASE_URL" not in daemon
    for field in ("daemon_pid", "daemon_started_at", "child_pid", "child_started_at", "executable", "script_path", "port"):
        assert field in daemon

    monkeypatch.setenv("DATABASE_URL", "mysql://user:pass@db.example/mmg")
    monkeypatch.setenv("PUBLIC_URL", "https://gateway.example")
    settings = Settings(data_dir=tmp_path, secret_key="windows-config")
    assert settings.database_url.startswith("mysql+asyncmy://")
    assert settings.public_url == "https://gateway.example"


def test_manager_stops_only_verified_instance_and_redacts_display():
    manage = MANAGE.read_text(encoding="utf-8-sig")
    assert "Get-OwnedInstance" in manage
    assert "child_started_at" in manage
    assert "daemon_started_at" in manage
    assert "Stop-Process -Name mmg" not in manage
    assert "Get-Process -Name mmg" not in manage
    assert "[REDACTED]" in manage
    assert "DATABASE_URL" in manage
    assert "-WindowStyle Hidden" in manage
