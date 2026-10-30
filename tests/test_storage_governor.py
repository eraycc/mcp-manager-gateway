"""Cleanup leadership fails over and idle domains do not starve later batches."""
from types import SimpleNamespace

from mcp_manager.config import Settings
from mcp_manager.logs import LogStore
from mcp_manager.storage_governor import StorageGovernor


async def test_only_one_worker_collects_and_successor_takes_over(tmp_path, monkeypatch):
    import mcp_manager.storage_governor as module

    (tmp_path / "locks").mkdir()
    state = SimpleNamespace(config=Settings(data_dir=tmp_path, secret_key="governor"), logs=LogStore(tmp_path))
    calls = []

    async def recover(current, **kwargs):
        return True

    async def collect(current, **kwargs):
        calls.append(current)
        return {"deleted": 0, "by_kind": {"catalog_rows": 0}}

    monkeypatch.setattr(module, "recover_cache_inventory", recover)
    monkeypatch.setattr(module, "run_storage_retention", collect)
    first, second = StorageGovernor(state), StorageGovernor(state)
    try:
        assert (await first.run_once())["batches"] == 5
        assert (await second.run_once())["status"] == "another_worker"
        assert len(calls) == 5
        await first.close()
        assert (await second.run_once())["batches"] == 5
        assert len(calls) == 10
    finally:
        await first.close()
        await second.close()
        await state.logs.close()


async def test_one_empty_domain_does_not_stop_cleanup_before_later_work(tmp_path, monkeypatch):
    import mcp_manager.storage_governor as module

    (tmp_path / "locks").mkdir()
    state = SimpleNamespace(config=Settings(data_dir=tmp_path, secret_key="governor"), logs=LogStore(tmp_path))
    count = 0

    async def recover(current, **kwargs):
        return False

    async def collect(current, **kwargs):
        nonlocal count
        count += 1
        value = 17 if count == 3 else 0
        return {"deleted": value, "by_kind": {"catalog_rows": value}}

    monkeypatch.setattr(module, "recover_cache_inventory", recover)
    monkeypatch.setattr(module, "run_storage_retention", collect)
    governor = StorageGovernor(state)
    try:
        result = await governor.run_once(row_budget=1000)
        assert result["batches"] == 4 and result["deleted"] == 17
        assert result["status"] == "inventory_rebuilding"
    finally:
        await governor.close()
        await state.logs.close()
