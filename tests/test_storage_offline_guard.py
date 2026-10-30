"""Destructive cache cutover requires verified files and cannot be emitted blindly."""
from io import StringIO

import pytest
from alembic import command
from alembic.config import Config

from mcp_manager.config import PACKAGE_ROOT


@pytest.mark.parametrize("url", [
    "sqlite:///offline.db",
    "mysql+asyncmy://example:placeholder@localhost/mmg_test_offline",
])
def test_offline_cutover_requires_verified_storage_migration(tmp_path, monkeypatch, url):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)
    output = StringIO()
    config = Config(output_buffer=output)
    config.set_main_option("script_location", str(PACKAGE_ROOT / "migrations"))
    with pytest.raises(RuntimeError, match="mmg storage migrate"):
        command.upgrade(config, "0010:head", sql=True)
    assert "DROP TABLE" not in output.getvalue()
    assert "SET version_num='0011'" not in output.getvalue()
