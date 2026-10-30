import pytest

from mcp_manager.logs import LogStore


@pytest.mark.asyncio
async def test_jsonl_index_rebuild_and_user_scope(tmp_path):
    logs = LogStore(tmp_path)
    await logs.append({"id": "a", "user_id": "u1", "username": "alice", "status": "success", "tool_name": "echo", "arguments": {"password": "secret"}})
    await logs.append({"id": "b", "user_id": "u2", "username": "bob", "status": "tool_error", "tool_name": "delete"})
    page = await logs.query(user_id="u1")
    assert page["total"] == 1
    assert (await logs.detail("a", user_id="u1"))["arguments"]["password"] == "[REDACTED]"
    assert await logs.detail("b", user_id="u1") is None
    await logs.rebuild()
    assert (await logs.query())["total"] == 2
    await logs.close()


@pytest.mark.asyncio
async def test_delete_physically_removes_all_call_events(tmp_path):
    logs = LogStore(tmp_path)
    await logs.append({"id": "a", "user_id": "u", "status": "running"})
    await logs.append({"id": "a", "user_id": "u", "status": "success"})
    await logs.append({"id": "b", "user_id": "u", "status": "success"})
    assert await logs.delete(["a"]) == 1
    await logs.rebuild()
    assert await logs.detail("a") is None
    assert (await logs.query())["total"] == 1
    assert (await logs.stats())["calls"] == 1
    await logs.close()


@pytest.mark.asyncio
async def test_search_and_pagination(tmp_path):
    logs = LogStore(tmp_path)
    for i in range(6):
        await logs.append({"id": str(i), "user_id": "u", "tool_name": "echo", "status": "success", "duration_ms": 10})
    page = await logs.query(q="echo", page=2, page_size=2)
    assert page["total_pages"] == 3
    assert len(page["items"]) == 2
    await logs.close()

@pytest.mark.parametrize("filtered", [False, True])
async def test_deleting_last_call_removes_empty_day_and_allows_new_writes(tmp_path, filtered):
    logs = LogStore(tmp_path)
    try:
        await logs.append({"id": "a", "timestamp": "2026-09-01T00:00:00Z", "status": "running"})
        await logs.append({"id": "a", "timestamp": "2026-09-01T00:01:00Z", "status": "success"})
        await logs.append({"id": "b", "timestamp": "2026-09-02T00:00:00Z", "status": "success"})
        empty_day = logs.calls_root / "2026-09-01.jsonl"
        if filtered:
            assert await logs.delete(to_time="2026-09-01T23:59:59Z") == 1
        else:
            assert await logs.delete(["a"]) == 1
        assert not empty_day.exists()
        assert (await logs.detail("b"))["id"] == "b"
        await logs.rebuild()
        assert await logs.detail("a") is None
        await logs.append({"id": "c", "timestamp": "2026-09-01T02:00:00Z", "status": "success"})
        assert (await logs.detail("c"))["id"] == "c"
        assert await logs.delete() == 2
        assert not logs.sources.paths()
        # Older versions may have left empty files even when the index is empty.
        empty_day.touch()
        legacy_empty_day = logs.root / "2026-09-01.jsonl"
        legacy_empty_day.touch()
        assert await logs.delete() == 0
        assert not empty_day.exists()
        assert not legacy_empty_day.exists()
    finally:
        await logs.close()
