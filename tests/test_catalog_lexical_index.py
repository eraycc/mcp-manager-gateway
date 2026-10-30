"""Portable lexical catalog search is built once at publication time."""
import pytest
from sqlalchemy import func, select

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.catalog_models import CatalogTerm
from mcp_manager.catalog_store import CatalogStore
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.database import McpServer
from mcp_manager.derived_models import index_key


async def make_store(dual_db, db_settings):
    async with dual_db.locked() as session:
        row = McpServer(
            name="Search service",
            slug="search-service",
            transport="stdio",
            config={},
            isolation="service",
        )
        session.add(row)
        await session.flush()
    return CatalogStore(
        dual_db,
        ArtifactStore(db_settings.data_dir),
        signing_key=db_settings.jwt_secret,
    ), row


@pytest.mark.asyncio
async def test_keyword_search_reuses_index_and_chinese_semantics(dual_db, db_settings, monkeypatch):
    store, row = await make_store(dual_db, db_settings)
    scope = CatalogScope(row.id, "service", row.revision, "shared")
    generation = await store.begin_refresh(scope)
    tools = [
        {
            "name": "weatherForecast",
            "description": "全球天气预报与空气质量",
            "inputSchema": {
                "type": "object",
                "properties": {"城市": {"type": "string", "description": "查询城市"}},
            },
        },
        *[
            {
                "name": f"common-tool-{index:03d}",
                "description": "公共 检索 候选",
                "inputSchema": {"type": "object"},
            }
            for index in range(249)
        ],
    ]
    await store.stage_page(generation.id, "tools", tools)
    await store.publish(generation.id, expected_scope=scope)

    async with dual_db.session() as session:
        persisted_terms = await session.scalar(
            select(func.count()).select_from(CatalogTerm).where(
                CatalogTerm.generation_id == index_key(generation.id)
            )
        )
    assert persisted_terms > 0

    import mcp_manager.catalog_retrieval as retrieval

    monkeypatch.setattr(
        retrieval,
        "index_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("request-time document tokenization")
        ),
    )
    chinese_total, chinese = await store.query_tool_entries(
        [generation.id], query="天气 预报", limit=200
    )
    assert chinese_total == 1
    assert [entry.original_name for entry in chinese] == ["weatherForecast"]

    normalized_total, normalized = await store.query_tool_entries(
        [generation.id], query="ＷＥＡＴＨＥＲ forecast", limit=200
    )
    assert normalized_total == 1
    assert [entry.original_name for entry in normalized] == ["weatherForecast"]

    common_total, common = await store.query_tool_entries(
        [generation.id], query="公共", limit=500
    )
    assert common_total == 200
    assert len(common) == 200
    assert [entry.original_name for entry in common] == sorted(
        entry.original_name for entry in common
    )
