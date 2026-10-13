"""Batched retention coordinator for database rows and immutable artifacts."""
from datetime import timedelta

from sqlalchemy import delete, exists, select

from .catalog_models import ArtifactObject, CatalogEntry, CatalogGeneration, CatalogHead, ProposalTestEntry
from .database import McpProposal
from .job_models import JobResult


async def _prune_capability_rows(state, at, limit):
    if limit <= 0:
        return 0
    cutoff = at - timedelta(hours=24)
    async with state.db.locked() as session:
        protected = set((await session.scalars(select(CatalogHead.generation_id).where(
            CatalogHead.generation_id.is_not(None)))).all())
        protected.update((await session.scalars(select(CatalogHead.refresh_id).where(
            CatalogHead.refresh_id.is_not(None)))).all())
        stale = select(CatalogGeneration.id).where(CatalogGeneration.created_at < cutoff)
        if protected:
            stale = stale.where(CatalogGeneration.id.notin_(protected))
        ids = list((await session.scalars(select(CatalogEntry.id).where(
            CatalogEntry.generation_id.in_(stale)).limit(limit))).all())
        if ids:
            await session.execute(delete(CatalogEntry).where(CatalogEntry.id.in_(ids)))
            return len(ids)
        generations = list((await session.scalars(stale.where(~exists().where(
            CatalogEntry.generation_id == CatalogGeneration.id)).limit(limit))).all())
        if generations:
            await session.execute(delete(CatalogGeneration).where(CatalogGeneration.id.in_(generations)))
        return len(generations)


async def _prune_proposal_entries(state, at, limit):
    if limit <= 0:
        return 0
    cutoff = at - timedelta(hours=24)
    async with state.db.locked() as session:
        active_runs = select(McpProposal.test_run_id).where(
            McpProposal.test_run_id.is_not(None), McpProposal.test_status.in_(("queued", "running")))
        ids = list((await session.scalars(select(ProposalTestEntry.id).where(
            ProposalTestEntry.created_at < cutoff,
            ProposalTestEntry.run_id.notin_(active_runs),
        ).limit(limit))).all())
        if ids:
            await session.execute(delete(ProposalTestEntry).where(ProposalTestEntry.id.in_(ids)))
        return len(ids)


async def _prune_artifacts(state, limit):
    if limit <= 0:
        return 0
    # SQL anti-joins select only bounded orphan candidates; never materialize all live ids.
    async with state.db.session() as session:
        ids = list((await session.scalars(select(ArtifactObject.id).where(
            ~exists().where(CatalogEntry.artifact_id == ArtifactObject.id),
            ~exists().where(ProposalTestEntry.artifact_id == ArtifactObject.id),
            ~exists().where(JobResult.artifact_id == ArtifactObject.id),
        ).limit(limit))).all())
    removed = await state.catalog.artifacts.discard(ids, limit=limit)
    if not removed:
        return 0
    async with state.db.locked() as session:
        # Recheck references after filesystem work; FK constraints remain the final guard.
        deletable = list((await session.scalars(select(ArtifactObject.id).where(
            ArtifactObject.id.in_(removed),
            ~exists().where(CatalogEntry.artifact_id == ArtifactObject.id),
            ~exists().where(ProposalTestEntry.artifact_id == ArtifactObject.id),
            ~exists().where(JobResult.artifact_id == ArtifactObject.id),
        ))).all())
        if deletable:
            await session.execute(delete(ArtifactObject).where(ArtifactObject.id.in_(deletable)))
        return len(deletable)


async def run_storage_retention(state, *, at, batch_limit=250):
    if type(batch_limit) is not int or not 1 <= batch_limit <= 250:
        raise ValueError("Retention batch must contain 1–250 items")
    deleted = {}
    remaining = batch_limit
    deleted["jobs"] = await state.jobs.store.prune(at=at, batch_limit=remaining)
    remaining -= deleted["jobs"]
    deleted["proposal_entries"] = await _prune_proposal_entries(state, at, remaining)
    remaining -= deleted["proposal_entries"]
    deleted["catalog_rows"] = await _prune_capability_rows(state, at, remaining)
    remaining -= deleted["catalog_rows"]
    deleted["artifacts"] = await _prune_artifacts(state, remaining)
    return {"deleted": sum(deleted.values()), "by_kind": deleted}
