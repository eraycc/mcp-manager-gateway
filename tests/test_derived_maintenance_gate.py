"""Maintenance excludes all derived repositories, with fast busy results."""
import importlib.util


def test_active_repository_defers_maintenance_and_closed_owner_is_reaped(tmp_path):
    assert importlib.util.find_spec("mcp_manager.storage_gate"), "Need shared derived admission"
    from mcp_manager.storage_gate import StorageGate
    first, second = StorageGate(tmp_path), StorageGate(tmp_path)
    try:
        with first.operation(), second.maintenance() as admitted:
            assert not admitted
        with second.maintenance() as admitted:
            assert admitted
    finally:
        first.close()
        second.close()
    assert not list((tmp_path / "locks/derived-workers").glob("*.lease"))


async def test_derived_session_and_cache_share_maintenance_boundary(dual_db, db_settings):
    from mcp_manager.derived_cache import DerivedCache
    cache = DerivedCache(db_settings.data_dir, "catalog")
    try:
        async with dual_db.derived.locked():
            with cache.gate.maintenance() as admitted:
                assert not admitted
        with cache.gate.maintenance() as admitted:
            assert admitted
            from sqlalchemy import text
            async with dual_db.derived.session() as session:
                assert await session.scalar(text("SELECT 1")) == 1
    finally:
        cache.close()
