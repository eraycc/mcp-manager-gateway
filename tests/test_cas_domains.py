"""Durable and cache domains have independent roots and interprocess exclusion."""
import asyncio

from mcp_manager.artifact_store import ArtifactStore


async def test_domains_duplicate_content_but_do_not_share_cleanup(tmp_path):
    cache = ArtifactStore(tmp_path, gc_grace_seconds=0)
    durable = cache.for_namespace("durable")
    left = await cache.put_json({"value": "same"}, max_bytes=1024)
    right = await durable.put_json({"value": "same"}, max_bytes=1024)
    assert left.id == right.id and left.namespace != right.namespace
    await cache.discard([left.id])
    assert await durable.read_json(right, max_bytes=1024) == {"value": "same"}


async def test_root_attachment_excludes_an_independent_store_collector(tmp_path):
    writer = ArtifactStore(tmp_path, gc_grace_seconds=0)
    collector = ArtifactStore(tmp_path, gc_grace_seconds=0)
    ref = await writer.put_json({"contract": "safe"}, max_bytes=1024)
    async with writer.hold([ref]):
        deleting = asyncio.create_task(collector.discard([ref.id]))
        await asyncio.sleep(0.05)
        assert not deleting.done()
        assert writer._path(ref.id).is_file()
    assert await deleting == [ref.id]


async def test_attachment_rejects_missing_body_before_root_commit(tmp_path):
    store = ArtifactStore(tmp_path, gc_grace_seconds=0)
    ref = await store.put_json({"value": 1}, max_bytes=1024)
    await store.discard([ref.id])
    import pytest

    from mcp_manager.runtime import GatewayError
    with pytest.raises(GatewayError, match="missing"):
        async with store.hold([ref]):
            raise AssertionError("Must not commit a dangling root")
