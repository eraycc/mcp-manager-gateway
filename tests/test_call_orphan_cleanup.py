"""Bounded call cleanup preserves active roots, epoch fences and unmanaged files."""
from uuid import uuid4

from mcp_manager.call_cleanup import collect_call_orphans
from mcp_manager.logs import LogStore


async def test_call_sweep_cleans_orphans_and_dead_fences_in_bounded_steps(tmp_path):
    logs = LogStore(tmp_path)
    try:
        identity = await logs.append({"status": "success", "result": "x" * 100000})
        rooted = [path for path in logs.payload_root.glob("*/*/*.json")]
        orphan = logs._payload_path("a" * 64)
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_text("{}", encoding="utf-8")
        unknown = logs.payload_root / "unmanaged.json"
        unknown.write_text("{}", encoding="utf-8")
        missing_day = logs.calls_root / "2020-01-01.jsonl"
        logs.sources.bump(missing_day)
        metadata = logs.sources._metadata_path(missing_day)
        dead = logs.sources.workers / (uuid4().hex + ".lock")
        dead.touch()
        for _ in range(30):
            report = collect_call_orphans(logs, limit=1)
            assert report["scanned"] <= 1
            if not orphan.exists() and not metadata.exists() and not dead.exists():
                break
        assert not orphan.exists() and not metadata.exists() and not dead.exists()
        assert unknown.exists() and logs.epochs.path.exists()
        assert all(path.exists() for path in rooted)
        assert (await logs.detail(identity))["result"] == "x" * 100000
    finally:
        await logs.close()
