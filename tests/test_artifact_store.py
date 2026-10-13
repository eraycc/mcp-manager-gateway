"""Immutable artifacts must be bounded, crash-safe, verified and streamed."""
import hashlib
import importlib.util
import json

import pytest

from mcp_manager.runtime import GatewayError


def artifact_store(tmp_path, **kwargs):
    assert importlib.util.find_spec("mcp_manager.artifact_store"), "Large objects need a separate bounded store"
    from mcp_manager.artifact_store import ArtifactStore
    return ArtifactStore(tmp_path, **kwargs)


async def test_utf8_artifact_roundtrip_and_stream(tmp_path):
    store = artifact_store(tmp_path)
    value = {"name": "工具😀", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}}
    ref = await store.put_json(value, max_bytes=1024)
    raw = b"".join([chunk async for chunk in store.open_download(ref)])
    assert hashlib.sha256(raw).hexdigest() == ref.sha256 == ref.id
    assert len(raw) == ref.size_bytes
    assert json.loads(raw) == value
    assert await store.read_json(ref, max_bytes=1024) == value


async def test_oversize_object_is_never_published(tmp_path):
    store = artifact_store(tmp_path)
    with pytest.raises(GatewayError) as caught:
        await store.put_json({"value": "中" * 1000}, max_bytes=100)
    assert caught.value.code == "artifact_too_large"
    assert not list((tmp_path / "objects").rglob("*.json"))
    assert not list((tmp_path / "objects").rglob("*.tmp"))


async def test_interrupted_replace_leaves_no_published_reference(tmp_path, monkeypatch):
    store = artifact_store(tmp_path)
    def fail(*args):
        raise OSError("Synthetic interrupted replace")
    monkeypatch.setattr("mcp_manager.artifact_store.os.replace", fail)
    with pytest.raises(OSError):
        await store.put_json({"value": "test"}, max_bytes=1024)
    assert not list((tmp_path / "objects").rglob("*.json"))
    assert not list((tmp_path / "objects").rglob("*.tmp"))


async def test_invalid_reference_cannot_escape_object_root(tmp_path):
    store = artifact_store(tmp_path)
    from mcp_manager.catalog_types import ArtifactRef
    with pytest.raises(ValueError):
        await store.read_json(ArtifactRef("../private", 1, "bad"), max_bytes=1024)


async def test_content_corruption_is_detected(tmp_path):
    store = artifact_store(tmp_path, cache_bytes=0)
    ref = await store.put_json({"value": "test"}, max_bytes=1024)
    path = next((tmp_path / "objects").rglob("*.json"))
    path.write_bytes(b'{"value":"evil"}')
    with pytest.raises(GatewayError) as caught:
        await store.read_json(ref, max_bytes=1024)
    assert caught.value.code == "artifact_corrupt"


async def test_cached_object_still_obeys_read_budget_and_value_isolation(tmp_path):
    store = artifact_store(tmp_path)
    ref = await store.put_json({"nested": {"name": "original"}}, max_bytes=1024)
    loaded = await store.read_json(ref, max_bytes=1024)
    loaded["nested"]["name"] = "mutated"
    assert (await store.read_json(ref, max_bytes=1024))["nested"]["name"] == "original"
    with pytest.raises(GatewayError) as caught:
        await store.read_json(ref, max_bytes=1)
    assert caught.value.code == "artifact_too_large"


async def test_gc_is_bounded_and_preserves_live_objects(tmp_path):
    store = artifact_store(tmp_path, gc_grace_seconds=0)
    refs = [await store.put_json({"value": index}, max_bytes=1024) for index in range(20)]
    live = {ref.id for ref in refs[:10]}
    removed = await store.gc(live, limit=5)
    assert 0 <= removed <= 5
    assert len(list((tmp_path / "objects").rglob("*.json"))) >= 15
    for ref in refs[:10]:
        assert await store.read_json(ref, max_bytes=1024) == {"value": refs.index(ref)}


async def test_active_stream_is_pinned_during_gc(tmp_path):
    store = artifact_store(tmp_path, gc_grace_seconds=0)
    ref = await store.put_json({"value": "a" * 150000}, max_bytes=200000)
    stream = store.open_download(ref)
    first = await anext(stream)
    assert 0 < len(first) <= 65536
    assert await store.gc(set(), limit=250) == 0
    await stream.aclose()
    assert await store.gc(set(), limit=250) == 1


async def test_cancellation_during_stream_open_releases_pin(tmp_path, monkeypatch):
    import asyncio
    import threading

    store = artifact_store(tmp_path, gc_grace_seconds=0)
    ref = await store.put_json({"value": "test"}, max_bytes=1024)
    entered, release, done = asyncio.Event(), threading.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    original = store._open_verified
    def delayed(reference):
        result = original(reference)
        loop.call_soon_threadsafe(entered.set)
        release.wait(5)
        loop.call_soon_threadsafe(done.set)
        return result
    monkeypatch.setattr(store, "_open_verified", delayed)
    stream = store.open_download(ref)
    task = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(done.wait(), 5)
    # Close is completed before the cancelled opening operation returns.
    assert await store.gc(set(), limit=250) == 1
