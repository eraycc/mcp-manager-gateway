"""An immutable verified memory body stays readable while disk collection is fenced."""
import asyncio

import pytest

from mcp_manager.artifact_store import ArtifactStore


@pytest.mark.parametrize("namespace", ["cache", "durable"])
async def test_verified_memory_body_does_not_wait_on_a_disk_collector(dual_db, namespace):
    store = ArtifactStore(dual_db.settings.data_dir, namespace=namespace)
    ref = await store.put_json({"tool": "exact-schema"}, max_bytes=4096)
    assert await store.read_json(ref, max_bytes=4096) == {"tool": "exact-schema"}
    guard = store._stripe(ref.id)
    guard.acquire()
    task = asyncio.create_task(store.read_json(ref, max_bytes=4096))
    try:
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "A pinned immutable memory value waited for a disk-only collector"
        assert task.result() == {"tool": "exact-schema"}
    finally:
        guard.release()
        await task
        store.close()


async def test_root_pin_disk_validation_keeps_event_loop_responsive(dual_db, monkeypatch):
    import threading
    import time
    from types import SimpleNamespace

    store = ArtifactStore(dual_db.settings.data_dir)
    ref = await store.put_json({"value": "contract"}, max_bytes=4096)
    original = store._path
    entered, release = threading.Event(), threading.Event()
    def slow_path(*args):
        path = original(*args)
        def stat():
            entered.set()
            release.wait(2)
            return path.stat()
        return SimpleNamespace(stat=stat)
    monkeypatch.setattr(store, "_path", slow_path)
    async def pin():
        async with store.hold([ref]):
            pass
    started = time.perf_counter()
    task = asyncio.create_task(pin())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert time.perf_counter() - started < 1, "Synchronous pin verification blocked the event loop"
    finally:
        release.set()
        await task
        store.close()


async def test_root_pin_verifies_every_body_before_committing_any_root(dual_db):
    from mcp_manager.runtime import GatewayError

    store = ArtifactStore(dual_db.settings.data_dir)
    first = await store.put_json({"value": "missing-first"}, max_bytes=4096)
    second = await store.put_json({"value": "present-last"}, max_bytes=4096)
    store._path(first.id).unlink()
    try:
        with pytest.raises(GatewayError, match="Artifact body is missing"):
            async with store.hold([first, second]):
                pytest.fail("Only the final reference was checked")
    finally:
        store.close()
