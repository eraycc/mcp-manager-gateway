"""Large tool schemas remain whole and every bounded page can be traversed."""
import pytest

from mcp_manager import tool_index
from mcp_manager.runtime import GatewayError


def test_byte_budget_paginates_without_truncating_schema(monkeypatch):
    monkeypatch.setattr(tool_index, "MAX_PAGE_BYTES", 700)
    items = [{"gateway_name": str(i), "inputSchema": {"description": "x" * 300}} for i in range(5)]
    cursor = None
    found = []
    while True:
        page = tool_index.paginate(items, cursor=cursor, limit=100, version="v", key="test")
        assert len(page["items"]) <= 2
        found.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert found == items


def test_oversized_single_schema_has_named_error(monkeypatch):
    monkeypatch.setattr(tool_index, "MAX_PAGE_BYTES", 100)
    with pytest.raises(GatewayError, match="large__tool") as caught:
        tool_index.paginate([{"gateway_name": "large__tool", "inputSchema": {"description": "x" * 200}}],
                            cursor=None, limit=20, version="v", key="test")
    assert caught.value.code == "catalog_entry_too_large"
