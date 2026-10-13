"""Diagnostic UI requests summary-only payloads for very large tool sets."""
from contextlib import asynccontextmanager

from test_discovery_ingestion import PagedClient

from mcp_manager.transports import SdkConnection


async def test_large_diagnostic_response_is_summary_only(db_app, monkeypatch):
    tools = [
        {
            "name": f"tool-{index:04}",
            "description": "large tool",
            "inputSchema": {"type": "object", "description": "x" * 2_000},
        }
        for index in range(1_000)
    ]

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
    assert response.json()["tool_count"] == 1_000
    assert "tools" not in response.json()
