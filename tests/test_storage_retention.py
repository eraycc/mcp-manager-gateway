"""Retention is batched and keeps every active database reference."""
import os
from datetime import timedelta

from sqlalchemy import select
from test_catalog_paged_api import indexed_catalog

from mcp_manager.catalog_models import ArtifactObject
from mcp_manager.database import now
from mcp_manager.job_models import JobRecord
from mcp_manager.retention import run_storage_retention


async def test_expiry_keeps_active_jobs_and_current_catalog_objects(db_app):
    state = db_app.app.state
    row, tools = await indexed_catalog(db_app, 1)
    active = await state.jobs.store.create_job("active", 1, user_id=db_app.actor["id"])
    await state.jobs.store.set_status(active["id"], "running")
    await state.jobs.store.append_result(active["id"], 0, {"item": 0, "ok": True, "result": {"active": True}})
    expired = await state.jobs.store.create_job("expired", 1, user_id=db_app.actor["id"])
    await state.jobs.store.append_result(expired["id"], 0, {"item": 0, "ok": True, "result": {"old": True}})
    await state.jobs.store.set_status(expired["id"], "completed")
    orphan = await state.catalog.artifacts.put_json({"orphan": True}, max_bytes=1024)
    async with state.db.locked() as session:
        session.add(ArtifactObject(id=orphan.id, size_bytes=orphan.size_bytes, sha256=orphan.sha256))
        old = await session.get(JobRecord, expired["id"])
        old.updated_at = now() - timedelta(days=8)
    orphan_path = state.catalog.artifacts._path(orphan.id)
    os.utime(orphan_path, (0, 0))
    state.catalog.artifacts.gc_grace_seconds = 0

    result = await run_storage_retention(state, at=now(), batch_limit=250)

    assert result["deleted"] <= 250
    assert await state.jobs.store.get_status(active["id"]) is not None
    assert await state.jobs.store.get_status(expired["id"]) is None
    assert await state.catalog.tool_definition(row, tools[0]["name"]) == tools[0]
    assert not orphan_path.exists()
    async with state.db.session() as session:
        assert await session.scalar(select(ArtifactObject.id).where(ArtifactObject.id == orphan.id)) is None
