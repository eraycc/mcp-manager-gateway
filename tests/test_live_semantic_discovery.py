"""Opt-in real OpenAI-compatible retrieval; credentials come only from the environment."""
import os

import pytest
from test_progressive_discovery import SCHEMA, discovery_context, payload

from mcp_manager.embedding_api import load_embedding_config
from mcp_manager.tool_index import document, rank

pytestmark = pytest.mark.skipif(not os.getenv("MCP_LIVE_EMBEDDING_KEY"), reason="Live embedding key not supplied")


async def test_live_embeddings_rank_multilingual_queries_and_discovery(tmp_path):
    config = {
        "enabled": True,
        "base_url": os.getenv("MCP_LIVE_EMBEDDING_URL", "https://api.siliconflow.cn"),
        "api_key": os.environ["MCP_LIVE_EMBEDDING_KEY"],
        "model": os.getenv("MCP_LIVE_EMBEDDING_MODEL", "BAAI/bge-m3"),
        "timeout_seconds": 30,
        "min_similarity": 0.5,
    }
    docs = [
        document("read", ["read_file"], [("Read local file contents by filesystem path.", 2)]),
        document("mail", ["send_email"], [("Send an email notification to a recipient.", 2)]),
        document("build", ["build_status"], [("List Jenkins build history and job execution status.", 2)]),
        document("sql", ["select_rows"], [("Query relational database using a SELECT statement.", 2)]),
    ]
    queries = [("查看文本文件里的内容", "read"), ("发邮件通知同事", "mail"),
               ("看看持续集成任务执行结果", "build"), ("Retrieve rows from a database", "sql")]
    async with discovery_context(tmp_path) as (app, _rows, calls, _ctx, invoke, web):
        saved = await web.patch("/api/v1/settings/embedding", json=config)
        assert saved.status_code == 200
        assert saved.json()["api_key"] == "[REDACTED]"
        probe = (await web.post("/api/v1/settings/embedding/test")).json()
        assert probe["status"] == "ready", probe
        assert probe["dimension"] > 0
        loaded = await load_embedding_config(app.state)
        for query, expected in queries:
            matches, status = await rank(docs, query, app.state.embeddings, loaded, "live-evaluation")
            assert status["semantic_status"] == "ready", status
            assert matches[0][0]["id"] == expected, [(d["id"], score) for d, score in matches]
        services = payload(await invoke("gateway_search_mcps", {"query": "存储文件"}))
        assert services["search"]["services"]["semantic_status"] == "ready", services
        assert services["items"][0]["slug"] == "files", services
        found = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "读取文档内容"}))
        assert found["search"]["tools"]["semantic_status"] == "ready", found
        assert found["items"][0]["gateway_name"] == "files__read_file", found
        assert found["items"][0]["inputSchema"] == SCHEMA
        # Exact lookup bypasses vectors even with the configured real provider.
        exact = payload(await invoke("gateway_search_tools", {"mcp": "files", "tool": "read_file"}))
        assert exact["items"][0]["match"]["match_type"] == "exact"
        assert exact["search"]["tools"]["semantic_status"] == "not_needed"
        result = await invoke("gateway_call", exact["items"][0]["examples"][0])
        assert not result.is_error
        assert len(calls) == 1
        # Semantic page traversal must remain valid after vectors enter the cache.
        args = {"mcp": "*", "tool": "读取文档内容", "limit": 1}
        first = payload(await invoke("gateway_search_tools", args))
        cursor = first["next_cursor"]
        seen = [first["items"][0]["gateway_name"]]
        while cursor:
            following = payload(await invoke("gateway_search_tools", args | {"cursor": cursor}))
            seen.extend(x["gateway_name"] for x in following["items"])
            cursor = following["next_cursor"]
        assert len(seen) == len(set(seen)) == first["total"]
