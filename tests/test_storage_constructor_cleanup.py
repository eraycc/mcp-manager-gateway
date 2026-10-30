"""Failed repository construction releases its own database and OS leases."""
import pytest

from mcp_manager.app import create_app
from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.config import Settings
from mcp_manager.derived_cache import DerivedCache


def assert_no_leases(root):
    assert not list((root / "locks/derived-workers").glob("*.lease"))
    assert not list((root / "locks/staging-workers").glob("*.lock"))


def test_artifact_inventory_failure_releases_admission_gate(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("inventory initialization failed")
    monkeypatch.setattr("mcp_manager.object_inventory.ObjectInventory", fail)
    with pytest.raises(RuntimeError, match="initialization"):
        ArtifactStore(tmp_path)
    assert_no_leases(tmp_path)


def test_cache_artifact_failure_closes_connection_and_gate(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("artifact initialization failed")
    monkeypatch.setattr("mcp_manager.derived_cache.ArtifactStore", fail)
    with pytest.raises(RuntimeError, match="initialization"):
        DerivedCache(tmp_path, "catalog")
    assert_no_leases(tmp_path)


async def test_catalog_index_failure_closes_both_artifact_owners(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("catalog index initialization failed")
    monkeypatch.setattr("mcp_manager.catalog.CatalogStore", fail)
    app = create_app(Settings(data_dir=tmp_path, secret_key="constructor-cleanup"))
    with pytest.raises(RuntimeError, match="initialization"):
        async with app.router.lifespan_context(app):
            raise AssertionError("Broken repository reached ready")
    assert_no_leases(tmp_path)
