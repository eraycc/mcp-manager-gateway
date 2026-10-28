"""Diagnostic UI requests summary-only payloads for very large tool sets."""
import json
from contextlib import asynccontextmanager

from catalog_fixture import oversized_tool_definitions
from test_discovery_ingestion import PagedClient

from mcp_manager.transports import SdkConnection


async def test_large_diagnostic_response_is_summary_only(db_app, monkeypatch):
    tools = oversized_tool_definitions()
    assert len(json.dumps(tools).encode()) > 1024 * 1024

    @asynccontextmanager
    async def connector(spec):
        yield SdkConnection(PagedClient([{"tools": tools}]), spec.config)

    monkeypatch.setattr(db_app.app.state.runtime, "connector", connector)
    response = await db_app.web.post(
        "/api/v1/mcps/diagnose",
        json={
            "transport": "stdio",
            "isolation": "service",
            "config": {"command": "synthetic"},
            "summary": True,
        },
    )
    assert response.status_code == 200, response.text
    assert len(response.content) <= 16_384
    assert response.json()["tool_count"] == len(tools)
    assert "tools" not in response.json()
