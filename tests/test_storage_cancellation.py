"""Cancellation waits for storage threads before releasing owners or closing stores."""
import asyncio
import threading

import pytest


async def test_cancelled_write_joins_worker_even_if_cancelled_again():
    from mcp_manager.storage_async import run_blocking
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def write():
        entered.set()
        release.wait(5)
        finished.set()
        return 17

    task = asyncio.create_task(run_blocking(write))
    await asyncio.to_thread(entered.wait, 2)
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_gate_cancellation_releases_admission_before_close(tmp_path, monkeypatch):
    from mcp_manager.storage_gate import StorageGate
    gate = StorageGate(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = gate._enter

    def delayed():
        original()
        entered.set()
        release.wait(5)

    monkeypatch.setattr(gate, "_enter", delayed)

    async def operation():
        async with gate.async_operation():
            raise AssertionError("Cancelled before entering caller")

    task = asyncio.create_task(operation())
    await asyncio.to_thread(entered.wait, 2)
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.close()
    assert gate.count == 0
