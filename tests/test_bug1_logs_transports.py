import pytest

from mcp_manager.logs import LogStore
from mcp_manager.transports import substitute, validate_config


async def test_log_time_filters_and_day_groups_use_configured_timezone(tmp_path):
    store = LogStore(tmp_path, timezone_name="Asia/Shanghai")
    try:
        await store.append({"id": "midnight", "timestamp": "2026-09-11T16:30:00+00:00", "status": "success"})
        await store.append({"id": "before", "timestamp": "2026-09-11T15:30:00Z", "status": "success"})
        result = await store.query(from_time="2026-09-12T00:00", to_time="2026-09-12T01:00")
        assert [r["id"] for r in result["items"]] == ["midnight"]
        assert (await store.query(from_time="2026-09-12T00:00:00+08:00"))["total"] == 1
        assert (await store.stats())["daily"] == [
            {"day": "2026-09-11", "calls": 1, "success": 1},
            {"day": "2026-09-12", "calls": 1, "success": 1}]
    finally:
        await store.close()


def test_null_nested_rest_parameter_is_actionable():
    with pytest.raises(ValueError, match="user.name"):
        substitute("{user.name}", {"user": None})


def test_oauth_missing_authorization_endpoint_is_rejected_on_save():
    with pytest.raises(ValueError, match="authorization_url"):
        validate_config("streamable-http", {"url": "https://mcp.test",
            "auth": {"type": "oauth", "token_url": "https://auth.test/token"}})


def test_conflicting_auth_headers_rejected_case_insensitively():
    with pytest.raises(ValueError, match="header"):
        validate_config("streamable-http", {"url": "https://mcp.test", "headers": {"x-api-key": "one"},
            "auth": {"type": "api_key", "header": "X-API-Key", "value": "two"}})


async def test_log_export_keyset_does_not_skip_after_deletion(tmp_path):
    store = LogStore(tmp_path)
    try:
        for i in range(5):
            await store.append({"id": str(i), "timestamp": f"2026-09-12T00:00:0{i}+00:00",
                                "status": "success", "user_id": "mine", "arguments": {"n": i}})
        page, cursor = await store.export_batch({"user_id": "mine"}, limit=2)
        assert [x["id"] for x in page] == ["4", "3"]
        await store.delete(["4"])
        page, cursor = await store.export_batch({"user_id": "mine"}, cursor=cursor, limit=2)
        assert [x["id"] for x in page] == ["2", "1"]
        assert page[0]["arguments"] == {"n": 2}
        assert (await store.export_batch({"user_id": "another"}))[0] == []
    finally:
        await store.close()
