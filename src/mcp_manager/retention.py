"""Fair bounded retention with authoritative roots checked under object locks."""
import asyncio
from datetime import timedelta

from filelock import Timeout
from sqlalchemy import and_, delete, exists, func, or_, select, text, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from .catalog_manifests import manifest_pointer
from .catalog_models import (
    ArtifactObject,
    CatalogEntry,
    CatalogGeneration,
    CatalogHead,
    CatalogManifest,
    CatalogObjectRef,
    CatalogTerm,
    CatalogWord,
    ProposalTestEntry,
)
from .catalog_types import ArtifactRef
from .database import McpProposal
from .embedding_models import EmbeddingVector
from .embeddings import prune_stale_vectors
from .job_models import JobResult
from .storage_async import run_blocking


async def _prune_capability_rows(state, at, limit):
    if limit <= 0:
        return 0
    protected = exists().where(or_(
        CatalogHead.generation_id == CatalogGeneration.id,
        CatalogHead.pending_id == CatalogGeneration.id,
        and_(CatalogHead.refresh_id == CatalogGeneration.id,
             CatalogGeneration.created_at >= at - timedelta(hours=1)),
    ))
    stale = select(CatalogGeneration.id).where(~protected)
    stale_keys = select(CatalogGeneration.index_id).where(~protected)
    turn = getattr(state, "_catalog_gc_turn", 0) % 5
    state._catalog_gc_turn = turn + 1
    pointer = None
    async with state.db.derived.locked() as session:
        if turn == 0:
            identities = (await session.execute(select(
                CatalogTerm.generation_id, CatalogTerm.entry_id, CatalogTerm.term_id
            ).where(CatalogTerm.generation_id.in_(stale_keys)).limit(limit))).all()
            if identities:
                await session.execute(delete(CatalogTerm).where(tuple_(
                    CatalogTerm.generation_id, CatalogTerm.entry_id, CatalogTerm.term_id).in_(identities)))
            return len(identities)
        if turn == 1:
            identities = (await session.execute(select(
                EmbeddingVector.generation_id, EmbeddingVector.entry_id, EmbeddingVector.model_hash
            ).where(EmbeddingVector.generation_id.in_(stale)).limit(limit))).all()
            if identities:
                await session.execute(delete(EmbeddingVector).where(tuple_(
                    EmbeddingVector.generation_id, EmbeddingVector.entry_id,
                    EmbeddingVector.model_hash).in_(identities)))
            return len(identities)
        if turn == 2:
            identities = list((await session.scalars(select(CatalogEntry.id).where(
                CatalogEntry.generation_id.in_(stale),
                ~exists().where(CatalogTerm.entry_id == CatalogEntry.index_id),
                ~exists().where(EmbeddingVector.entry_id == CatalogEntry.id),
            ).limit(limit))).all())
            if identities:
                await session.execute(delete(CatalogEntry).where(CatalogEntry.id.in_(identities)))
            return len(identities)
        if turn == 3:
            identities = list((await session.scalars(stale.where(
                ~exists().where(CatalogEntry.generation_id == CatalogGeneration.id)).limit(limit))).all())
            if identities:
                await session.execute(delete(CatalogGeneration).where(CatalogGeneration.id.in_(identities)))
            return len(identities)
        newer = aliased(CatalogManifest)
        rank = select(func.count()).select_from(newer).where(
            newer.scope_key == CatalogManifest.scope_key, newer.current.is_(False),
            or_(newer.created_at > CatalogManifest.created_at,
                and_(newer.created_at == CatalogManifest.created_at, newer.id > CatalogManifest.id))
        ).correlate(CatalogManifest).scalar_subquery()
        manifest = await session.scalar(select(CatalogManifest).where(
            CatalogManifest.current.is_(False),
            or_(CatalogManifest.retire_after < at, rank >= 2),
            ~exists().where(CatalogHead.pending_id == CatalogManifest.id),
            ~exists().where(CatalogHead.refresh_id == CatalogManifest.id,
                            CatalogManifest.created_at >= at - timedelta(hours=1)),
        ).order_by(CatalogManifest.created_at).limit(1))
        if not manifest:
            return 0
        identities = list((await session.scalars(select(CatalogObjectRef.artifact_id).where(
            CatalogObjectRef.manifest_id == manifest.id).limit(limit))).all())
        if identities:
            await session.execute(delete(CatalogObjectRef).where(
                CatalogObjectRef.manifest_id == manifest.id, CatalogObjectRef.artifact_id.in_(identities)))
            if len(identities) == limit:
                return len(identities)
        pointer = manifest_pointer(state.db.settings.data_dir, manifest.scope_key, manifest.id)
        await session.delete(manifest)
    await asyncio.to_thread(pointer.unlink, missing_ok=True)
    return len(identities) + 1


async def drain_retired_postings(state, *, at, limit=5000):
    """One short SQL transaction; no live-body traversal or Python posting graph."""
    if type(limit) is not int or not 1 <= limit <= 5000:
        raise ValueError("Invalid retired posting budget")
    async with state.db.derived.locked() as session:
        result = await session.execute(text("""DELETE FROM catalog_terms WHERE rowid IN(
            SELECT t.rowid FROM catalog_terms t
            WHERE t.generation_id IN(
                SELECT g.index_id FROM catalog_generations g
                WHERE NOT EXISTS(SELECT 1 FROM catalog_heads h WHERE h.generation_id=g.id)
                  AND NOT EXISTS(SELECT 1 FROM catalog_heads h WHERE h.pending_id=g.id)
                  AND NOT EXISTS(SELECT 1 FROM catalog_heads h WHERE h.refresh_id=g.id AND g.created_at>=:recent))
            LIMIT :budget)"""), {"recent": (at - timedelta(hours=1)).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f"), "budget": limit})
        return result.rowcount


async def _prune_proposal_entries(state, at, limit):
    if limit <= 0:
        return 0
    cutoff = at - timedelta(hours=24)
    async with state.db.locked() as session:
        active_runs = select(McpProposal.test_run_id).where(
            McpProposal.test_run_id.is_not(None), McpProposal.test_status.in_(("queued", "running")))
        identities = list((await session.scalars(select(ProposalTestEntry.id).where(
            ProposalTestEntry.created_at < cutoff, ProposalTestEntry.run_id.notin_(active_runs),
        ).limit(limit))).all())
        if identities:
            await session.execute(delete(ProposalTestEntry).where(ProposalTestEntry.id.in_(identities)))
        return len(identities)


def _scan_objects(store, limit):
    # Iterator state advances even if every examined object is live.
    with store._lock:
        if store._gc_cursor is None:
            store._gc_cursor = store._object_paths()
        result = []
        for _ in range(limit):
            path = next(store._gc_cursor, None)
            if path is None:
                store._gc_cursor = None
                break
            result.append(path)
        return result


async def collect_objects(state, *, namespace="cache", limit=250):
    if type(limit) is not int or not 1 <= limit <= 250:
        raise ValueError("Invalid collection budget")
    store = state.jobs.store.artifacts if namespace == "durable" else state.catalog.artifacts
    removed = 0
    if namespace == "cache" and not (await asyncio.to_thread(store.inventory.ready, "cache")
            and await asyncio.to_thread(store.inventory.roots_ready, "cache")):
        return 0
    for path in await asyncio.to_thread(_scan_objects, store, limit):
        identity = path.stem
        if store._pins[(namespace, identity)]:
            continue
        try:
            size = (await asyncio.to_thread(path.stat)).st_size
            ref = ArtifactRef(identity, size, identity, namespace)
            async with store.hold([ref], verify=False, timeout=0):
                if namespace == "durable":
                    async with state.db.session() as session:
                        referenced = await session.scalar(select(or_(
                            exists().where(JobResult.artifact_id == identity),
                            exists().where(ProposalTestEntry.artifact_id == identity))))
                else:
                    async with state.db.derived.session() as session:
                        referenced = await session.scalar(select(or_(
                            exists().where(CatalogEntry.artifact_id == identity),
                            exists().where(EmbeddingVector.artifact_id == identity),
                            exists().where(CatalogObjectRef.artifact_id == identity),
                            exists().where(CatalogManifest.artifact_id == identity))))
                        referenced |= bool(await session.scalar(text(
                            "SELECT 1 FROM cache_items WHERE artifact_id=:identity LIMIT 1"), {"identity": identity}))
                if referenced:
                    continue
                deleted = await asyncio.to_thread(store._discard, [identity])
                if deleted and namespace == "cache":
                    async with state.db.derived.locked() as session:
                        await session.execute(delete(ArtifactObject).where(ArtifactObject.id == identity))
                removed += len(deleted)
        except (Timeout, FileNotFoundError):
            continue
        except SQLAlchemyError:
            # A failed authority read is never evidence that a durable root is absent.
            state.storage_gc_error = "authority_unavailable"
            break
    return removed


async def _prune_artifacts(state, limit):
    return await collect_objects(state, namespace="cache", limit=limit) if limit else 0


async def _prune_words(state, limit):
    if not limit:
        return 0
    async with state.db.derived.locked() as session:
        identities = list((await session.scalars(select(CatalogWord.id).where(
            ~exists().where(CatalogTerm.term_id == CatalogWord.id)).limit(limit))).all())
        if identities:
            await session.execute(delete(CatalogWord).where(CatalogWord.id.in_(identities)))
        return len(identities)


async def run_storage_retention(state, *, at, batch_limit=250):
    if type(batch_limit) is not int or not 1 <= batch_limit <= 250:
        raise ValueError("Retention batch must contain 1–250 items")
    actions = [
        ("jobs", lambda count: state.jobs.store.prune(at=at, batch_limit=count)),
        ("proposal_entries", lambda count: _prune_proposal_entries(state, at, count)),
        ("catalog_rows", lambda count: _prune_capability_rows(state, at, count)),
        ("artifacts", lambda count: collect_objects(state, namespace="cache", limit=count)),
        ("durable_artifacts", lambda count: collect_objects(state, namespace="durable", limit=count)),
        ("words", lambda count: _prune_words(state, count)),
    ]
    turn = getattr(state, "_storage_gc_turn", 0) % len(actions)
    state._storage_gc_turn = turn + 1
    actions = actions[turn:] + actions[:turn]
    quotient, remainder = divmod(batch_limit, len(actions))
    deleted = {}
    for index, (name, action) in enumerate(actions):
        budget = quotient + int(index < remainder)
        deleted[name] = await action(budget) if budget else 0
    return {"deleted": sum(deleted.values()), "by_kind": deleted}


async def reconcile_service_cache(state, server_id=None, *, startup=False):
    """Only startup or a concrete catalog/model change initiates cache reconciliation."""
    total, cached, cache_after = 0, 0, ""
    while True:
        result = await prune_stale_vectors(state, batch_limit=250, server_id=server_id,
                                          startup=startup, cache_after=cache_after)
        total += result["vectors"]
        cached += result["cache_entries"]
        cache_after = result["cache_next"]
        if result["vectors"] < 250 and not cache_after:
            break
        await asyncio.sleep(0)

    async with state.db.session() as session:
        from .database import McpServer

        services = dict((await session.execute(select(McpServer.id, McpServer.revision))).all())

    catalog_entries, after = 0, ""
    while True:
        page = await run_blocking(state.catalog.cache_store.metadata_page, after=after, limit=250)
        doomed = [key for key, metadata in page
                  if (server_id is None or key.startswith(server_id + "-"))
                  and (key.rsplit("-", 1)[0] not in services
                       or metadata.get("revision") != services.get(key.rsplit("-", 1)[0]))]
        if doomed:
            catalog_entries += await run_blocking(state.catalog.cache_store.delete_keys, doomed)
        if len(page) < 250:
            break
        after = page[-1][0]
        await asyncio.sleep(0)
    return {"vectors": total, "embedding_entries": cached, "catalog_entries": catalog_entries}


async def run_startup_cache_cleanup(state):
    """Batched historical cleanup; retries only on failure, never on an hourly timer."""
    return await reconcile_service_cache(state, startup=True)


async def cache_changed(state, server_id=None):
    """Keep successful publication valid even if a physical rewrite needs a retry."""
    try:
        await reconcile_service_cache(state, server_id)
    except OSError:
        state.cache_cleanup_done = False
        state.retention_next = 0
