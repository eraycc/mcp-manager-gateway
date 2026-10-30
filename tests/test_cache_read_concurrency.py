"""Immutable compatibility reads must not synchronously wait for a collector stripe."""
import pytest
from filelock import FileLock

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.derived_cache import DerivedCache


@pytest.mark.parametrize("warm", [False, True])
async def test_cache_reads_return_immutable_bytes_while_the_object_stripe_is_held(tmp_path, monkeypatch, warm):
    cache = DerivedCache(tmp_path, "catalog", max_cache_bytes=1024)
    collector = ArtifactStore(tmp_path)
    try:
        cache.set("summary", {"value": "ready"})
        identity = cache.connection.execute(
            "SELECT artifact_id FROM cache_items WHERE key='summary'").fetchone()[0]
        if warm:
            assert cache.get("summary") == {"value": "ready"}
        guard = collector._stripe(identity)
        waiting_path = str(guard.lock_file)
        monkeypatch.setattr(cache.artifacts, "_stripe",
                            lambda _: FileLock(waiting_path, timeout=.01, thread_local=False))
        with guard:
            assert cache.get("summary") == {"value": "ready"}
    finally:
        collector.close()
        cache.close()
