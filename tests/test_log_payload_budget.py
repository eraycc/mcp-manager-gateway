"""Large log payloads live outside the query projection and remain directly retrievable."""
import json

import pytest

from mcp_manager.logs import LogStore, log_summary_and_ref


@pytest.mark.asyncio
async def test_log_list_never_loads_large_payload_and_preview_is_bounded(tmp_path, monkeypatch):
    store = LogStore(tmp_path)
    event = {
        "id": "large-log",
        "user_id": "user-1",
        "token_id": "token-1",
        "mcp_id": "mcp-1",
        "tool_name": "large",
        "status": "success",
        "result": {"text": "数据" * 600_000},
    }
    summary = log_summary_and_ref(event)
    assert len(json.dumps(summary["summary"], ensure_ascii=False).encode()) <= 65536
    assert summary["payload"] is not None

    await store.append(event)
    payloads = list((tmp_path / "logs/calls/payloads").glob("*/*/*.json"))
    assert len(payloads) == 1
    daily = next((tmp_path / "logs/calls").glob("????-??-??.jsonl"))
    assert daily.stat().st_size <= 65536

    def forbidden_payload_read(*_args, **_kwargs):
        raise AssertionError("list/stats must not load payload artifacts")

    monkeypatch.setattr(store, "_read_payload", forbidden_payload_read)
    page = await store.query(page_size=20)
    stats = await store.stats(user_id="user-1")
    assert page["items"][0]["id"] == "large-log"
    assert stats["calls"] == 1
    assert len(store._stats_cache) == 1
    assert await store.stats(user_id="user-1") == stats

    monkeypatch.undo()
    preview = await store.preview("large-log", user_id="user-1", max_bytes=65536)
    assert preview["truncated"] is True
    assert len(preview["preview"].encode()) <= 65536
    detail = await store.detail("large-log", user_id="user-1")
    assert detail["result"] == event["result"]

    assert await store.delete(["large-log"]) == 1
    assert not payloads[0].exists()
    await store.close()
