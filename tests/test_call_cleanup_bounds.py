"""Physical call cleanup keeps unaffected days and retires modern append fences."""
import json

import pytest

from mcp_manager.logs import LogStore


async def test_cleanup_only_rewrites_affected_days_and_releases_all_payload_versions(tmp_path):
    logs = LogStore(tmp_path)
    try:
        for status in ("running", "success"):
            await logs.append({"id": "removed", "timestamp": "2026-10-28T00:00:00+00:00",
                               "status": status, "result": status * 70000})
        await logs.append({"id": "keep", "timestamp": "2026-10-29T00:00:00+00:00", "status": "success"})
        untouched = logs.calls_root / "2026-10-29.jsonl"
        original = untouched.stat()
        # Its bytes and source generation must remain untouched as well.
        generation = logs.sources.generation(untouched)
        assert await logs.delete(["removed"]) == 1
        assert untouched.stat().st_mtime_ns == original.st_mtime_ns
        assert logs.sources.generation(untouched) == generation
        assert not list(logs.payload_root.glob("*/*/*.json"))
        assert (await logs.query())["total"] == 1
        assert (await logs.detail("keep"))["id"] == "keep"
    finally:
        await logs.close()


async def test_modern_deleted_ids_need_no_permanent_journal_with_live_peer(tmp_path):
    logs, peer = LogStore(tmp_path), LogStore(tmp_path)
    try:
        identity = await logs.append({"status": "running"})
        continuing = await peer.append({"status": "running"})
        assert logs.epochs.birth(identity) is not None
        await logs.delete([identity])
        await peer.append({"id": identity, "status": "success", "result": "x" * 70000})
        await peer.append({"id": continuing, "status": "success"})
        assert await logs.detail(identity) is None
        assert (await logs.detail(continuing))["status"] == "success"
        assert len(logs.deleted) == 0
        assert not list(logs.tombstones.glob("*.jsonl"))
        assert not list(logs.payload_root.glob("*/*/*.json"))
        for path in logs.sources.paths():
            assert all(json.loads(line)["id"] != identity for line in path.read_text(encoding="utf-8").splitlines())
    finally:
        await peer.close()
        await logs.close()


async def test_missing_epoch_metadata_fails_closed_after_index_reconstruction(tmp_path):
    logs = LogStore(tmp_path)
    await logs.append({"status": "success"})
    await logs.close()
    (tmp_path / "logs/calls/.sources/epochs.json").unlink()
    with pytest.raises(RuntimeError, match="epoch"):
        LogStore(tmp_path)


async def test_copying_a_daily_source_keeps_same_worker_and_peer_appends_available(tmp_path, monkeypatch):
    import asyncio
    import threading

    from mcp_manager.call_rewrite import CallRewriter

    logs, peer = LogStore(tmp_path), LogStore(tmp_path)
    entered, proceed = threading.Event(), threading.Event()
    original = CallRewriter._copy_batch
    def paused(self, *args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert proceed.wait(10), "Test did not release copy"
        return original(self, *args, **kwargs)
    monkeypatch.setattr(CallRewriter, "_copy_batch", paused)
    deleting = None
    try:
        await logs.append({"id": "removed", "status": "success"})
        keep = await logs.append({"status": "success"})
        deleting = asyncio.create_task(logs.delete(["removed"]))
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            for writer in (logs, peer):
                await asyncio.wait_for(writer.append({"status": "success"}), timeout=2)
            assert (await logs.detail(keep))["status"] == "success"
        finally:
            proceed.set()
        assert await deleting == 1
        assert (await logs.query())["total"] == 3
        for item in (await logs.query())["items"]:
            assert (await logs.detail(item["id"]))["id"] == item["id"]
    finally:
        proceed.set()
        if deleting is not None:
            await asyncio.gather(deleting, return_exceptions=True)
        await peer.close()
        await logs.close()


async def test_failed_sql_cutover_repairs_offsets_and_retains_recoverable_decision(tmp_path, monkeypatch):
    from mcp_manager.call_rewrite import CallRewriter

    logs = LogStore(tmp_path)
    await logs.append({"id": "removed", "status": "success"})
    keep = await logs.append({"status": "success", "result": "preserved"})
    def fail(*args):
        raise OSError("injected cutover failure")
    monkeypatch.setattr(CallRewriter, "_cutover", fail)
    try:
        with pytest.raises(OSError, match="cutover failure"):
            await logs.delete(["removed"])
        assert await logs.detail("removed") is None
        assert (await logs.detail(keep))["result"] == "preserved"
        assert list(logs.tombstones.glob("*.jsonl"))
        assert not list(logs.calls_root.glob("*.tmp"))
        assert logs.conn.execute("SELECT count(*) FROM call_rewrite_offsets").fetchone()[0] == 0
    finally:
        await logs.close()
    restarted = LogStore(tmp_path)
    try:
        assert await restarted.detail("removed") is None
        assert (await restarted.detail(keep))["result"] == "preserved"
        assert not list(restarted.tombstones.glob("*.jsonl"))
    finally:
        await restarted.close()


async def test_legacy_tombstone_limit_fails_before_new_durable_deletion(tmp_path):
    from mcp_manager.runtime import GatewayError
    from mcp_manager.storage_admission import StorageAdmission

    logs, peer = LogStore(tmp_path), LogStore(tmp_path)
    admission = StorageAdmission(tmp_path)
    try:
        admission.set_policy({"legacy_call_tombstones": 1})
        await logs.append({"id": "first", "status": "success"})
        await logs.append({"id": "second", "status": "success"})
        await logs.delete(["first"])
        with pytest.raises(GatewayError) as failure:
            await logs.delete(["second"])
        assert failure.value.code == "legacy_tombstone_capacity"
        assert (await logs.detail("second"))["status"] == "success"
        assert len(logs.deleted) == 1
    finally:
        admission.close()
        await peer.close()
        await logs.close()


async def test_statistics_invalidate_immediately_after_a_peer_commit(tmp_path):
    logs, peer = LogStore(tmp_path), LogStore(tmp_path)
    try:
        assert (await logs.stats())["calls"] == 0
        await peer.append({"status": "success"})
        assert (await logs.stats())["calls"] == 1
        await peer.delete()
        assert (await logs.stats())["calls"] == 0
    finally:
        await peer.close()
        await logs.close()


async def test_decoding_a_large_detail_does_not_hold_the_source_writer_fence(tmp_path, monkeypatch):
    import asyncio
    import threading

    logs = LogStore(tmp_path)
    entered, proceed = threading.Event(), threading.Event()
    read = None
    try:
        identity = await logs.append({"status": "success", "result": "x" * 100000})
        original = logs._decode_reference
        def slow(reference):
            entered.set()
            assert proceed.wait(10)
            return original(reference)
        monkeypatch.setattr(logs, "_decode_reference", slow)
        read = asyncio.create_task(logs.detail(identity))
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            await asyncio.wait_for(logs.append({"status": "success"}), timeout=2)
        finally:
            proceed.set()
        assert (await read)["result"] == "x" * 100000
    finally:
        proceed.set()
        if read is not None:
            await asyncio.gather(read, return_exceptions=True)
        await logs.close()
