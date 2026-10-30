"""Deletion decisions survive source failures and are retired after safe recovery."""
import json

import pytest

from mcp_manager.logs import LogStore


def event(identity):
    return {"id": identity, "timestamp": "2026-10-30T00:00:00+00:00", "status": "success"}


async def test_restart_finishes_pending_physical_deletion_before_projection(tmp_path, monkeypatch):
    logs = LogStore(tmp_path)
    await logs.append(event("removed"))
    await logs.append(event("keep"))
    original = logs._rewrite_deleted
    def interrupted(*args):
        raise OSError("injected before source rewrite")
    monkeypatch.setattr(logs, "_rewrite_deleted", interrupted)
    with pytest.raises(OSError, match="injected"):
        await logs.delete(["removed"])
    monkeypatch.setattr(logs, "_rewrite_deleted", original)
    await logs.close()
    restarted = LogStore(tmp_path)
    try:
        assert await restarted.detail("removed") is None
        rows = [json.loads(line) for path in restarted.sources.paths()
                for line in path.read_text(encoding="utf-8").splitlines()]
        assert {item["id"] for item in rows} == {"keep"}
        assert not list(restarted.tombstones.glob("*.jsonl"))
    finally:
        await restarted.close()


async def test_source_fence_is_durable_before_replacement(tmp_path, monkeypatch):
    import mcp_manager.logs as module
    logs = LogStore(tmp_path)
    try:
        await logs.append(event("removed"))
        await logs.append(event("keep"))
        source = logs.sources.paths()[0]
        previous = logs.sources.generation(source)
        original = module.os.replace
        seen = []
        def replacing(left, right):
            if str(left).endswith((".rewrite", ".tmp")) and str(right) == str(source):
                seen.append(logs.sources.generation(source))
            return original(left, right)
        monkeypatch.setattr(module.os, "replace", replacing)
        await logs.delete(["removed"])
        assert seen and all(value != previous for value in seen)
    finally:
        await logs.close()
