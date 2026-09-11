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
