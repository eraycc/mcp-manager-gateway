"""Approval removes diagnostic entries atomically, keeping unrelated snapshots."""
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select
from test_bug1_catalog import Connection
from test_proposal_atomicity import propose

from mcp_manager.catalog_models import ProposalTestEntry


@pytest.mark.parametrize("batch", [False, True])
async def test_approval_clears_only_its_diagnostic_snapshot(db_app, batch):
    @asynccontextmanager
    async def connect(spec):
        yield Connection("diagnostic")

    db_app.app.state.runtime.connector = connect
    identities = [
        await propose(db_app, name=name, config={"command": name})
        for name in ("approved", "untouched")
    ]
    for identity in identities:
        result = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/test?summary=true")
        assert result.status_code == 200, result.text
        assert result.json()["tool_count"] == 1
    identity, untouched = identities
    if batch:
        response = await db_app.web.post("/api/v1/mcp-proposals/batch", json={
            "action": "approve", "ids": [identity],
        })
        assert response.status_code == 200
        assert response.json()["results"][0]["ok"]
    else:
        response = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/approve", json={})
        assert response.status_code == 200, response.text
    async with db_app.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(ProposalTestEntry).where(
            ProposalTestEntry.proposal_id == identity
        )) == 0
        assert await session.scalar(select(func.count()).select_from(ProposalTestEntry).where(
            ProposalTestEntry.proposal_id == untouched
        )) == 1
    assert (await db_app.web.get(f"/api/v1/mcp-proposals/{identity}/test-capabilities")).status_code == 404
    assert (await db_app.web.get(f"/api/v1/mcp-proposals/{untouched}/test-capabilities")).status_code == 200
    detail = (await db_app.web.get(f"/api/v1/mcp-proposals/{identity}")).json()
    assert detail["status"] == "approved"
    assert detail["test_result"]["tool_count"] == 1
