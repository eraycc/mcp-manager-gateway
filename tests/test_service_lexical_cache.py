"""Service lexical work is single-flight and charged to the directory RAM budget."""
from mcp_manager.directory_snapshot import DirectorySnapshots


def test_auxiliary_scores_share_directory_byte_limit_and_version_eviction():
    cache = DirectorySnapshots(max_bytes=10000)
    assert cache.get_scores("one", "v1") is None
    cache.put_scores("one", "v1", ({"service": 1.5}, 2000))
    assert cache.get_scores("one", "v1") == {"service": 1.5}
    assert cache.bytes == 2000
    cache.put_scores("two", "v1", ({"other": 2.0}, 9000))
    assert cache.bytes <= 10000
    assert cache.get_scores("one", "v1") is None
    assert cache.get_scores("two", "v2") is None
    assert cache.bytes == 0
    cache.max_bytes = 0
    cache.put_scores("disabled", "v2", ({"service": 1}, 100))
    assert cache.bytes == 0


async def test_real_discovery_reuses_scores_and_recomputes_after_directory_change(db_app, monkeypatch):
    from test_catalog_paged_api import indexed_catalog
    from test_exact_route_scalability import gateway_context, invoke

    from mcp_manager import tool_index
    from mcp_manager.database import McpServer

    row, _tools = await indexed_catalog(db_app, 3)
    context, _ = await gateway_context(db_app)
    counted = []
    original = tool_index.lexical
    def counting(*args):
        counted.append(True)
        return original(*args)
    monkeypatch.setattr(tool_index, "lexical", counting)
    for _ in range(2):
        result = await invoke(db_app, context, "gateway_search_mcps", {"query": "indexed"})
        assert not result.is_error, result
    assert len(counted) == 1, "Real service discovery bypassed the bounded score cache"
    async with db_app.db.locked() as session:
        current = await session.get(McpServer, row.id)
        current.description = "changed indexed purpose"
    result = await invoke(db_app, context, "gateway_search_mcps", {"query": "indexed"})
    assert not result.is_error, result
    assert len(counted) == 2, "Directory changes reused stale service relevance"
    assert result.structured_content["total"] == 1
