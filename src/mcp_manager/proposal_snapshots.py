"""Owner/run/revision-bound diagnostic snapshots, never exposed as gateway routes."""
import asyncio
from datetime import timedelta

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from .catalog_models import ArtifactObject, ProposalTestEntry
from .catalog_store import CatalogStore, bounded_summary_rows, digest
from .catalog_types import ArtifactRef, CatalogPage, GenerationRef
from .database import McpProposal, now
from .db_types import strict_json_dumps
from .runtime import GatewayError


class ProposalSnapshotStore(CatalogStore):
    def __init__(self, db, artifacts, scope, *, signing_key):
        super().__init__(db, artifacts, signing_key=signing_key)
        self.scope = scope
        self.metadata = {}

    async def _check(self, session, *, published=False):
        scope = self.scope
        row = await session.get(McpProposal, scope.server_id, populate_existing=True)
        if (not row or row.status in {"approved", "rejected"} or row.revision != scope.revision
                or row.test_run_id != scope.credential_generation):
            raise GatewayError("generation_superseded", "Proposal test snapshot changed")
        if published and (
            row.test_status != "success" or row.tested_revision != scope.revision
            or (row.test_result or {}).get("snapshot_owner") != scope.owner
            or (row.test_result or {}).get("snapshot_run_id") != scope.credential_generation
            or not row.tested_at or row.tested_at < now() - timedelta(hours=24)
        ):
            raise GatewayError("catalog_not_found", "Proposal test snapshot is unavailable or expired")
        return row

    def _filters(self, kind=None):
        scope = self.scope
        values = [
            ProposalTestEntry.proposal_id == scope.server_id, ProposalTestEntry.actor_id == scope.owner,
            ProposalTestEntry.run_id == scope.credential_generation, ProposalTestEntry.revision == scope.revision,
        ]
        if kind:
            values.append(ProposalTestEntry.kind == kind)
        return values

    async def begin_refresh(self, scope):
        if scope != self.scope:
            raise ValueError("Snapshot scope mismatch")
        async with self.db.session() as session:
            await self._check(session)
        return GenerationRef(scope.credential_generation, scope, "", "staging")

    async def stage_page(self, identity, kind, entries):
        self._check_kind(kind)
        if identity != self.scope.credential_generation or len(entries) > 250:
            raise ValueError("Invalid diagnostic staging page")
        prepared = []
        for entry in entries:
            name = entry.get("uri") if kind == "resources" else entry.get("uriTemplate") if kind == "templates" else entry.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("Diagnostic capability has no exact name")
            ref = await self.artifacts.put_json(entry, max_bytes=8 * 1024 * 1024)
            summary = {"name": entry.get("name", name)}
            for key in ("description", "title"):
                if isinstance(entry.get(key), str):
                    summary[key] = entry[key][:1024]
            if kind in {"resources", "templates"}:
                summary["uri" if kind == "resources" else "uriTemplate"] = name
            prepared.append((ref, ProposalTestEntry(
                proposal_id=self.scope.server_id, actor_id=self.scope.owner,
                run_id=identity, revision=self.scope.revision, kind=kind, name=name,
                name_hash=digest(name), sort_key=name.encode()[:128].hex(), summary=summary,
                search_text=" ".join(str(summary.get(key, "")) for key in ("name", "title", "description", "uri", "uriTemplate")).casefold(),
                artifact_id=ref.id, artifact_bytes=ref.size_bytes,
            )))
        try:
            async with self.db.locked() as session:
                await self._check(session)
                ids = {ref.id for ref, _ in prepared}
                existing = set((await session.scalars(select(ArtifactObject.id).where(ArtifactObject.id.in_(ids)))).all())
                for ref, _ in prepared:
                    if ref.id not in existing:
                        session.add(ArtifactObject(id=ref.id, size_bytes=ref.size_bytes, sha256=ref.sha256))
                        existing.add(ref.id)
                await session.flush()
                session.add_all([entry for _, entry in prepared])
                await session.flush()
        except IntegrityError:
            raise GatewayError("catalog_duplicate_entry", "Diagnostic snapshot contains duplicate names") from None

    async def discard_kind(self, identity, kind):
        self._check_kind(kind)
        while True:
            async with self.db.locked() as session:
                await self._check(session)
                ids = list((await session.scalars(select(ProposalTestEntry.id).where(*self._filters(kind)).limit(250))).all())
                if not ids:
                    return
                await session.execute(delete(ProposalTestEntry).where(ProposalTestEntry.id.in_(ids)))

    async def set_metadata(self, identity, metadata):
        if await asyncio.to_thread(lambda: len(strict_json_dumps(metadata).encode())) > 8192:
            raise GatewayError("catalog_metadata_too_large", "Diagnostic metadata exceeds its byte budget")
        self.metadata = metadata

    async def publish(self, identity, *, expected_scope):
        if expected_scope != self.scope or identity != self.scope.credential_generation:
            raise ValueError("Diagnostic publish scope mismatch")
        async with self.db.locked() as session:
            await self._check(session)
        # The proposal's existing finish_test CAS is the visibility/approval boundary.
        return GenerationRef(identity, self.scope, "", "ready")

    async def fail_refresh(self, identity):
        # Keep invisible partial rows for bounded retention/cleanup; never replay the test.
        return None

    async def get_summary(self, scope):
        if scope != self.scope:
            raise ValueError("Diagnostic summary scope mismatch")
        async with self.db.session() as session:
            await self._check(session)
            counts = dict((await session.execute(select(
                ProposalTestEntry.kind, func.count()
            ).where(*self._filters()).group_by(ProposalTestEntry.kind))).all())
        return {
            "tool_count": counts.get("tools", 0), "resource_count": counts.get("resources", 0),
            "prompt_count": counts.get("prompts", 0), "template_count": counts.get("templates", 0),
            "snapshot_run_id": scope.credential_generation, "snapshot_owner": scope.owner,
            **self.metadata,
        }

    async def list_capabilities(self, scope, *, kind, query="", cursor=None, limit=50):
        self._check_kind(kind)
        if scope != self.scope or not 1 <= limit <= 50:
            raise ValueError("Invalid diagnostic capability query")
        query = query.casefold().strip()
        binding = digest(strict_json_dumps([scope.server_id, scope.owner, scope.revision,
                                           scope.credential_generation, kind, query, limit]))
        key = self._decode_cursor(cursor, binding) if cursor else None
        async with self.db.session() as session:
            await self._check(session, published=True)
            filters = self._filters(kind)
            if query:
                # Exact names filter without reading any Schema artifacts.
                pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                filters.append(ProposalTestEntry.search_text.like(pattern, escape="\\"))
            total = await session.scalar(select(func.count()).select_from(ProposalTestEntry).where(*filters))
            statement = select(ProposalTestEntry.summary, ProposalTestEntry.sort_key, ProposalTestEntry.name_hash).where(*filters)
            if key:
                statement = statement.where(or_(
                    ProposalTestEntry.sort_key > key[0],
                    and_(ProposalTestEntry.sort_key == key[0], ProposalTestEntry.name_hash > key[1])
                ))
            rows = (await session.execute(statement.order_by(
                ProposalTestEntry.sort_key, ProposalTestEntry.name_hash
            ).limit(limit + 1))).all()
        selected = await bounded_summary_rows(rows, limit)
        next_cursor = self._cursor({"binding": binding, "key": [selected[-1].sort_key, selected[-1].name_hash]}) if len(rows) > len(selected) else None
        async with self.db.session() as session:
            await self._check(session, published=True)
        return CatalogPage([dict(row.summary) for row in selected], total, next_cursor, scope.credential_generation)

    async def get_definition(self, scope, *, kind, name):
        self._check_kind(kind)
        async with self.db.session() as session:
            await self._check(session, published=True)
            row = await session.scalar(select(ProposalTestEntry).where(
                *self._filters(kind), ProposalTestEntry.name_hash == digest(name)
            ))
            if not row:
                return None
            if row.name != name:
                raise GatewayError("catalog_hash_collision", "Diagnostic name hash collision")
            ref = ArtifactRef(row.artifact_id, row.artifact_bytes, row.artifact_id)
        result = await self.artifacts.read_json(ref, max_bytes=8 * 1024 * 1024)
        async with self.db.session() as session:
            await self._check(session, published=True)
        return result
