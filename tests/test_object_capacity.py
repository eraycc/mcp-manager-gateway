"""Cache admission bounds orphan growth without spending durable storage reserves."""
import pytest

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.runtime import GatewayError


async def test_cache_quota_counts_unattached_bodies_and_releases_after_gc(tmp_path):
    store = ArtifactStore(tmp_path, gc_grace_seconds=0)
    store.quota_bytes = 80
    first = await store.put_json({"value": "a" * 40}, max_bytes=1024)
    with pytest.raises(GatewayError) as caught:
        await store.put_json({"value": "b" * 40}, max_bytes=1024)
    assert caught.value.code == "cache_capacity"
    await store.discard([first.id])
    assert await store.put_json({"value": "b" * 40}, max_bytes=1024)


async def test_cache_quota_does_not_delete_or_reject_a_durable_result(tmp_path):
    store = ArtifactStore(tmp_path, gc_grace_seconds=0)
    store.quota_bytes = 0
    durable = store.for_namespace("durable")
    ref = await durable.put_json({"irreplaceable": "evidence"}, max_bytes=1024)
    assert await durable.read_json(ref, max_bytes=1024) == {"irreplaceable": "evidence"}
