"""Reproduce two approvals passing the old pre-commit creation boundary."""
import asyncio

import pytest
from sqlalchemy import select

from mcp_manager.database import ApiToken, McpProposal, McpServer, User
from mcp_manager.proposals import submit_proposals
from mcp_manager.runtime import GatewayError


async def propose(harness, name="Concurrent approval", config=None):
    created = await harness.web.post("/api/v1/tokens", json={"name": "test", "enable_mcp_proposal": True})
    assert created.status_code == 200
    async with harness.db.session() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        token = await session.get(ApiToken, created.json()["id"])
    result = await submit_proposals(harness.app, user, token, {
        "name": name, "transport": "stdio", "config": config or {"command": "synthetic-never-launched"}
    })
    return result["items"][0]["id"]


async def test_double_approval_creates_exactly_one_service(db_app, monkeypatch):
    identity = await propose(db_app)
    catalog = db_app.app.state.catalog
    original = catalog.create
    arrivals, barrier = 0, asyncio.Event()

    async def delayed_create(*args, **kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 5)
        return await original(*args, **kwargs)

    monkeypatch.setattr(catalog, "create", delayed_create)
    responses = await asyncio.wait_for(asyncio.gather(*[
        db_app.web.post(f"/api/v1/mcp-proposals/{identity}/approve", json={"mode": "disabled"})
        for _ in range(2)
    ]), 10)
    assert all(response.status_code in {200, 409} for response in responses)
    async with db_app.db.session() as session:
        services = list((await session.scalars(select(McpServer))).all())
        proposal = await session.get(McpProposal, identity)
        assert len(services) == 1, "Approving one proposal must not leave a second orphan service"
        assert proposal.approved_mcp_id == services[0].id


async def test_failure_creating_credentials_rolls_back_service_and_approval(db_app, monkeypatch):
    identity = await propose(db_app)
    async def fail(*args, **kwargs):
        raise GatewayError("synthetic_failure", "Synthetic credential failure")
    monkeypatch.setattr(db_app.app.state.credentials, "persist_config", fail)
    response = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/approve", json={"mode": "disabled"})
    assert response.status_code >= 400
    async with db_app.db.session() as session:
        assert list((await session.scalars(select(McpServer))).all()) == []
        assert (await session.get(McpProposal, identity)).status == "pending"


async def test_refresh_observes_committed_approval(db_app, monkeypatch):
    identity = await propose(db_app)
    async def observe(row, user_id, authorize):
        async with db_app.db.session() as session:
            proposal = await session.get(McpProposal, identity)
            assert proposal.status == "approved", "Downstream I/O may only follow the atomic commit"
            assert proposal.approved_mcp_id == row.id
    monkeypatch.setattr(db_app.app.state.catalog, "refresh_after_change", observe)
    response = await db_app.web.post(f"/api/v1/mcp-proposals/{identity}/approve", json={})
    assert response.status_code == 200
