"""Explicit reconciliation imports legacy metadata once without replaying work."""
import json

import pytest

from mcp_manager.config import Settings
from mcp_manager.database import ApiToken, Database, McpProposal, McpServer, User
from mcp_manager.home_migration import reconcile_data
from mcp_manager.job_models import JobRecord


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
    jobs_dir = config.data_dir / "jobs"
    jobs_dir.mkdir()
    legacy_job = jobs_dir / "legacy-job.json"
    legacy_job.write_text(
        json.dumps({"id": "legacy-job", "action": "old.batch", "status": "running", "total": 3}),
        encoding="utf-8",
    )

    report = await reconcile_data(config, batch_size=2)
    assert report == {"proposals": 1, "catalog_snapshots": 1, "jobs": 1}
    assert not legacy_cache.exists()
    assert not legacy_job.exists()

    reopened = Database(config)
    await reopened.initialize()
    async with reopened.session() as session:
        proposal = await session.get(McpProposal, proposal_id)
        job = await session.get(JobRecord, "legacy-job")
        assert proposal.public_name == "公开名称"
        assert job.status == "interrupted"
        assert job.action == "old.batch"
    await reopened.close()

    assert await reconcile_data(config, batch_size=2) == {
        "proposals": 0,
        "catalog_snapshots": 0,
        "jobs": 0,
    }
