"""Explicit reconciliation imports legacy metadata once without replaying work."""
import json

import pytest

from mcp_manager.config import Settings
from mcp_manager.database import ApiToken, Database, McpProposal, McpServer, User
from mcp_manager.home_migration import reconcile_data



@pytest.mark.asyncio
async def test_reconcile_backfills_proposals_and_imports_legacy_indexes(tmp_path):
    config = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path}/state.sqlite",
        secret_key="reconcile-secret",
    )
    db = Database(config)
    await db.initialize()
    async with db.locked() as session:
        user = User(username="owner", password_hash="hash")
        server = McpServer(
            name="Legacy Service",
            slug="legacy-service",
            transport="stdio",
            config={},
        )
        session.add_all([user, server])
        await session.flush()
        token = ApiToken(
            user_id=user.id,
            name="agent",
            prefix="mcpm_legacy",
            token_hash="b" * 64,
        )
        session.add(token)
        await session.flush()
        proposal = McpProposal(
            user_id=user.id,
            token_id=token.id,
            payload={"name": "公开名称", "config": {"command": "legacy"}},
            public_name=None,
        )
        session.add(proposal)
        await session.flush()
        server_id = server.id
        proposal_id = proposal.id
    await db.close()

    cache_dir = config.data_dir / "cache"
    cache_dir.mkdir(parents=True)
    legacy_cache = cache_dir / f"{server_id}-1-shared.json"
    legacy_cache.write_text(
        json.dumps({"tools": [{"name": "legacy_tool"}], "cache_status": "ready"}),
        encoding="utf-8",
    )

    report = await reconcile_data(config, batch_size=2)
    assert report == {"proposals": 1, "catalog_snapshots": 1, "jobs": 0}
    assert not legacy_cache.exists()

    reopened = Database(config)
    await reopened.initialize()
    async with reopened.session() as session:
        proposal = await session.get(McpProposal, proposal_id)
        assert proposal.public_name == "公开名称"
    await reopened.close()

    assert await reconcile_data(config, batch_size=2) == {
        "proposals": 0,
        "catalog_snapshots": 0,
        "jobs": 0,
    }
