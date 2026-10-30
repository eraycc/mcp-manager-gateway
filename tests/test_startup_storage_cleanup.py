"""Partial initialization must release storage connections and the runtime owner."""
import sqlite3

import pytest
from filelock import FileLock

from mcp_manager.app import create_app
from mcp_manager.config import Settings


@pytest.mark.parametrize("phase", ["load_transport_plugins", "CredentialStore", "EmbeddingIndex"])
async def test_partial_startup_failure_releases_already_opened_storage(tmp_path, monkeypatch, phase):
    def fail(*args, **kwargs):
        raise RuntimeError("injected initialization failure")

    monkeypatch.setattr("mcp_manager.app." + phase, fail)
    app = create_app(Settings(data_dir=tmp_path, secret_key="startup-cleanup"))
    with pytest.raises(RuntimeError, match="injected initialization failure"):
        async with app.router.lifespan_context(app):
            raise AssertionError("Failed initialization cannot become ready")
    with FileLock(str(tmp_path / "runtime.lock"), timeout=0):
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        app.state.logs.conn.execute("SELECT 1")
    assert not list((tmp_path / "locks/derived-workers").glob("*.lease"))
