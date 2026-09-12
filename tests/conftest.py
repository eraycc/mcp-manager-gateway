"""Tests never inherit a real user's database or secret configuration."""
import pytest


@pytest.fixture(autouse=True)
def isolated_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_MANAGER_HOME", str(tmp_path / "config-home"))
    for name in ("DATABASE_URL", "DATA_DIR", "SECRET_KEY", "HOST", "PORT", "PUBLIC_URL", "COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolated_sse_shutdown(monkeypatch):
    # sse-starlette copies Uvicorn shutdown into a process-wide flag.
    # Each test starts its own server; a previous server's exit must not
    # immediately cancel the next test's SSE response.
    from sse_starlette.sse import AppStatus

    monkeypatch.setattr(AppStatus, "should_exit", False)
