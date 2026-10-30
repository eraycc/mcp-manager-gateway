"""Portable, paged catalog reads and short atomic generation publication."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections import OrderedDict

from sqlalchemy import String, and_, cast, delete, func, or_, select, union
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only

from .artifact_store import ArtifactStore
from .catalog_control import CatalogControl
from .catalog_models import ArtifactObject, CatalogEntry, CatalogGeneration, CatalogHead
from .catalog_types import ArtifactRef, CatalogPage, CatalogScope, GenerationRef, ToolRef
from .database import McpServer, now, uid
from .db_types import strict_json_dumps
from .derived_models import CatalogServer
from .derived_queries import ALLOWED, query_generations
from .runtime import GatewayError

KINDS = {"tools", "resources", "prompts", "templates"}
MAX_DEFINITION_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 8192


def _metadata_text(value, max_bytes):
    """Budget the serialized JSON string, including Unicode and escaped controls."""
    if not isinstance(value, str):
        return "[invalid metadata]", True
    sample = value[:max_bytes]
    if len(sample) == len(value) and len(strict_json_dumps(sample).encode("utf-8")) <= max_bytes:
        return sample, False
    low, high = 0, len(sample)
    while low < high:
        middle = (low + high + 1) // 2
        if len(strict_json_dumps(sample[:middle] + "…").encode("utf-8")) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return sample[:low] + "…", True


def metadata_summary(metadata):
    """Project oversized display metadata; complete capability contracts live separately."""
    summary = dict(metadata)
    if len(strict_json_dumps(summary).encode("utf-8")) <= MAX_METADATA_BYTES:
        return summary
    info = metadata.get("serverInfo")
    omitted = []
    if isinstance(info, dict) and "icons" in info:
        summary["serverInfo"] = {key: value for key, value in info.items() if key != "icons"}
        omitted.append("serverInfo.icons")
        summary.update(metadata_truncated=True, metadata_omitted_fields=list(omitted))
        if len(strict_json_dumps(summary).encode("utf-8")) <= MAX_METADATA_BYTES:
            return summary

    # Fixed JSON-byte budgets leave room for field names, counts and truncation markers.
    limits = {"name": 512, "version": 256, "title": 512, "description": 1024, "websiteUrl": 768}
    result = {"serverInfo": {}, "capability_errors": [], "metadata_truncated": True}
    truncated = []
    if isinstance(info, dict):
        for key, limit in limits.items():
            if key in info:
                text, clipped = _metadata_text(info[key], limit)
                if key == "websiteUrl" and clipped:
                    omitted.append("serverInfo.websiteUrl")  # Never synthesize a different navigation target.
                else:
                    result["serverInfo"][key] = text
                    if clipped:
                        truncated.append("serverInfo." + key)
        if set(info) - set(limits) - {"icons"}:
            omitted.append("serverInfo.extensions")
    if "protocolVersion" in metadata:
        result["protocolVersion"], clipped = _metadata_text(metadata["protocolVersion"], 128)
        if clipped:
            truncated.append("protocolVersion")
    if set(metadata) - {"serverInfo", "protocolVersion", "capability_errors", "metadata_omitted_fields"}:
        omitted.append("extensions")

    errors = metadata.get("capability_errors") or []
    if not isinstance(errors, list):
        errors = [{"capability": "metadata", "error": "Invalid capability error metadata"}]
    known, extra = {}, []
    for error in errors:
        item = error if isinstance(error, dict) else {
            "capability": "metadata", "error": "Invalid capability error entry"}
        capability = item.get("capability")
        if isinstance(capability, str) and capability in KINDS:
            known.setdefault(capability, item)
        elif len(extra) < 4:
            extra.append(item)
    selected = list(known.values()) + extra[:4 - len(known)]
    clipped_errors = len(selected) != len(errors)
    for item in selected:
        capability, clipped_name = _metadata_text(item.get("capability", ""), 64)
        message, clipped_message = _metadata_text(item.get("error", ""), 512)
        result["capability_errors"].append({"capability": capability, "error": message})
        clipped_errors |= clipped_name or clipped_message or bool(set(item) - {"capability", "error"})
    result["capability_errors_total"] = len(errors)
    result["capability_errors_truncated"] = clipped_errors
    if clipped_errors:
        truncated.append("capability_errors")
    if omitted:
        result["metadata_omitted_fields"] = omitted
    if truncated:
        result["metadata_truncated_fields"] = truncated
    return result


def tool_annotation_summary(annotations):
    """Keep display hints bounded without changing the full upstream definition."""
    if not isinstance(annotations, dict):
        return {}
    result = {key: annotations[key] for key in (
        "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"
    ) if type(annotations.get(key)) is bool}
    if isinstance(annotations.get("title"), str):
        result["title"] = annotations["title"][:256]
    return result


def searchable_text(entry):
    from .tool_index import normalize, schema_text

    text = " ".join(str(entry.get(key, "")) for key in ("name", "title", "description"))
    text += " " + schema_text(entry.get("inputSchema", {}))
    return normalize(text).encode("utf-8")[:32768].decode("utf-8", errors="ignore")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def scope_key(scope):
    return digest(strict_json_dumps([scope.server_id, scope.owner]))


def generation_scope(row):
    return CatalogScope(row.server_id, row.owner, row.revision, row.credential_generation)


def generation_ref(row):
    return GenerationRef(row.id, generation_scope(row), row.digest, row.status)


def tool_ref(row):
    return ToolRef(row.generation_id, row.original_name, row.gateway_name, row.schema_hash, row.artifact_id)


async def bounded_summary_rows(rows, limit, *, max_bytes=131072):
    def select_rows():
        selected, size = [], 4096  # Leave room for counters, generation and bounded signed cursor.
        for row in rows[:limit]:
            cost = len(strict_json_dumps(row.summary).encode("utf-8")) + 1
            if size + cost > max_bytes:
                if not selected:
                    raise GatewayError("catalog_entry_too_large", "Capability summary exceeds the page byte budget")
                break
            selected.append(row)
            size += cost
        return selected
    return await asyncio.to_thread(select_rows)


class CatalogStore:
    def __init__(self, db, artifacts: ArtifactStore, *, signing_key, scope_resolver=None):
        self.authority = db
        self.db = db.derived
        self.artifacts = artifacts
        self.signing_key = signing_key.encode() if isinstance(signing_key, str) else signing_key
        self.scope_resolver = scope_resolver
        self.on_publish = None
        self._search_cache = OrderedDict()
        self._search_cache_lock = asyncio.Lock()
        self._search_cache_entries = 64
        self._search_cache_bytes = 0
        self._search_sizes = {}
        self._search_cache_max_bytes = 8 * 1024 * 1024

    @staticmethod
    def _check_kind(kind):
        if kind not in KINDS:
            raise ValueError("Unknown capability kind")

    async def _validate_scope(self, session, scope):
        row = await session.get(McpServer, scope.server_id, populate_existing=True, options=[load_only(
            McpServer.id, McpServer.slug, McpServer.revision, McpServer.isolation, McpServer.mode
        )])
        if not row or row.revision != scope.revision:
            raise GatewayError("revision_changed", "MCP configuration changed during catalog refresh")
        if row.mode == "disabled":
            raise GatewayError("disabled", "MCP service is disabled")
        if self.scope_resolver:
            current = await self.scope_resolver(session, row, scope.owner)
            if current != scope:
                raise GatewayError("credential_changed", "Catalog credential scope changed")
        return row

    async def _control(self, session, scope, *, create=False):
        control = await session.get(CatalogControl, scope_key(scope), populate_existing=True)
        if control and (control.server_id, control.owner) != (scope.server_id, scope.owner):
            raise GatewayError("catalog_hash_collision", "Catalog scope hash collision")
        if control and (control.revision, control.credential_generation) != (
                scope.revision, scope.credential_generation):
            if not create:
                return None
            control.epoch += 1
            control.refresh_fence += 1
            control.revision, control.credential_generation = scope.revision, scope.credential_generation
            control.health, control.receipt = {}, {}
        if control is None and create:
            control = CatalogControl(scope_key=scope_key(scope), server_id=scope.server_id,
                                     owner=scope.owner, revision=scope.revision,
                                     credential_generation=scope.credential_generation)
            session.add(control)
            await session.flush()
        return control

    @staticmethod
    def _matches_receipt(control, generation):
        refresh = (control.receipt or {}).get("refresh", {}) if control else {}
        return bool(control and generation.control_epoch == control.epoch
                    and generation.refresh_fence == control.refresh_fence
                    and refresh.get("ticket") == generation.refresh_ticket
                    and refresh.get("state") in {"committed", "acknowledged"})

    async def _materialize(self, identity, expected_scope):
        # Retry only the committed projection, never the operation result.
        async with self.authority.locked() as primary:
            await self._validate_scope(primary, expected_scope)
            control = await self._control(primary, expected_scope)
            async with self.db.locked() as session:
                generation = await session.get(CatalogGeneration, identity, populate_existing=True)
                head = await session.get(CatalogHead, scope_key(expected_scope), populate_existing=True)
                if (not generation or not head or not self._matches_receipt(control, generation)
                        or head.refresh_id != identity):
                    return False
                if generation.status not in {"pending_control", "ready"}:
                    return False
                from datetime import timedelta

                from .catalog_models import CatalogManifest
                old = (await session.scalars(select(CatalogManifest).where(
                    CatalogManifest.scope_key == generation.scope_key,
                    CatalogManifest.current.is_(True), CatalogManifest.id != identity))).all()
                for manifest in old:
                    manifest.current = False
                    manifest.retire_after = now() + timedelta(hours=24)
                manifest = await session.get(CatalogManifest, identity)
                if manifest:
                    manifest.current = True
                generation.status = "ready"
                generation.published_at = generation.published_at or now()
                head.generation_id, head.status, head.pending_id = identity, "ready", None
                from .embedding_models import EmbeddingVector
                await session.execute(delete(EmbeddingVector).where(
                    EmbeddingVector.scope_key == generation.scope_key,
                    EmbeddingVector.generation_id != identity))
            receipt = dict(control.receipt or {})
            receipt["refresh"] = dict(receipt["refresh"], state="acknowledged")
            control.receipt, control.updated_at = receipt, now()
        return True

    async def _recover_scope(self, scope):
        async with self.db.session() as session:
            head = await session.get(CatalogHead, scope_key(scope))
            pending = head.pending_id if head else None
        if pending:
            await self._materialize(pending, scope)

    async def begin_refresh(self, scope):
        if not isinstance(scope, CatalogScope) or not scope.owner or len(scope.credential_generation) > 128:
            raise ValueError("Invalid catalog scope")
        await self._recover_scope(scope)
        async with self.authority.locked() as primary:
            await self._validate_scope(primary, scope)
            control = await self._control(primary, scope, create=True)
            control.refresh_fence += 1
            ticket, epoch, fence = uid(), control.epoch, control.refresh_fence
            receipt = dict(control.receipt or {})
            receipt["refresh"] = {"ticket": ticket, "state": "issued", "epoch": epoch,
                                  "fence": fence, "expires_at": time.time() + 3600}
            control.receipt, control.updated_at = receipt, now()
            from .catalog_operations import current_operation
            operation = current_operation.get()
            if operation and operation.scope == scope and operation.ticket:
                operation.ticket.update(epoch=epoch, fence=fence)
        async with self.db.locked() as session:
            server = await self._validate_scope(session, scope)
            projection = await session.get(CatalogServer, server.id)
            if projection is None:
                projection = CatalogServer(id=server.id)
                session.add(projection)
            for field in ("slug", "revision", "mode", "isolation"):
                setattr(projection, field, getattr(server, field))
            await session.flush()
            key = scope_key(scope)
            head = await session.get(CatalogHead, key)
            if head and (head.server_id != scope.server_id or head.owner != scope.owner):
                raise GatewayError("catalog_hash_collision", "Catalog scope hash collision")
            generation = CatalogGeneration(
                scope_key=key, server_id=scope.server_id, owner=scope.owner, revision=scope.revision,
                credential_generation=scope.credential_generation, status="staging", digest="", counts={},
                control_epoch=epoch, refresh_fence=fence, refresh_ticket=ticket,
            )
            session.add(generation)
            await session.flush()
            if head is None:
                head = CatalogHead(scope_key=key, server_id=scope.server_id, owner=scope.owner, status="empty")
                session.add(head)
            head.refresh_id = generation.id
            await session.flush()
            result = generation_ref(generation)
        if operation and operation.scope == scope:
            operation.refresh_version = result.id
        return result

    async def _staging(self, session, identity):
        generation = await session.get(CatalogGeneration, identity, populate_existing=True)
        if not generation:
            raise GatewayError("catalog_not_found", "Catalog generation not found")
        head = await session.get(CatalogHead, generation.scope_key, populate_existing=True)
        if not head or head.refresh_id != identity:
            raise GatewayError("generation_superseded", "A newer catalog refresh superseded this operation")
        if generation.status != "staging":
            raise GatewayError("generation_superseded", "Catalog generation no longer accepts entries")
        row = await self._validate_scope(session, generation_scope(generation))
        control = await self._control(session, generation_scope(generation))
        refresh = (control.receipt or {}).get("refresh", {}) if control else {}
        if (not control or control.epoch != generation.control_epoch
                or control.refresh_fence != generation.refresh_fence
                or refresh.get("ticket") != generation.refresh_ticket
                or refresh.get("expires_at", 0) <= time.time()):
            raise GatewayError("generation_superseded", "Refresh ticket was superseded or expired")
        return generation, head, row

    async def stage_page(self, identity, kind, entries):
        self._check_kind(kind)
        if not isinstance(entries, list) or len(entries) > 250:
            raise ValueError("A staging page may contain at most 250 entries")
        async with self.db.session() as session:
            _, _, row = await self._staging(session, identity)
            slug, server_id = row.slug, row.id
        from .catalog import alias

        prepared = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise TypeError("Capability entries must be objects")
            name = entry.get("uri") if kind == "resources" else entry.get("uriTemplate") if kind == "templates" else entry.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("Capability entry has no exact name or URI")
            ref = await self.artifacts.put_json(entry, max_bytes=MAX_DEFINITION_BYTES)
            summary = {"name": entry.get("name", name)}
            for field, limit in (("title", 256), ("description", 1024)):
                if isinstance(entry.get(field), str):
                    summary[field] = entry[field][:limit]
            if kind == "tools":
                annotations = tool_annotation_summary(entry.get("annotations"))
                if annotations:
                    summary["annotations"] = annotations
            if kind != "tools":
                from .embeddings import resource_vector_hash

                summary["semantic_hash"] = resource_vector_hash(entry)
            if kind in {"resources", "templates"}:
                summary["uri" if kind == "resources" else "uriTemplate"] = name
            gateway_name = alias(slug, name) if kind in {"tools", "prompts"} else None
            if gateway_name:
                summary["gateway_name"] = gateway_name
            schema_hash = await asyncio.to_thread(
                lambda definition=entry: digest(strict_json_dumps(definition.get("inputSchema", {})))
            ) if kind == "tools" else ""
            entry_id = uid()
            searchable = entry if kind == "tools" else {
                key: entry[key] for key in ("name", "title", "description") if key in entry
            }
            search_text = await asyncio.to_thread(searchable_text, searchable)
            values = {
                "id": entry_id, "generation_id": identity, "kind": kind,
                "original_name": name, "name_hash": digest(name),
                # A bounded ASCII sort prefix + full hash gives portable deterministic keyset pagination.
                "sort_key": name.encode("utf-8")[:128].hex(),
                "gateway_name": gateway_name, "gateway_hash": digest(gateway_name) if gateway_name else None,
                "legacy_gateway_hash": digest(server_id + "__" + name) if gateway_name else None,
                "schema_hash": schema_hash, "summary": summary, "search_text": search_text,
                "artifact_id": ref.id, "artifact_bytes": ref.size_bytes,
            }
            term_rows = []
            if kind == "tools":
                from .catalog_retrieval import index_document

                term_rows = await asyncio.to_thread(
                    index_document, identity,
                    {**entry, "entry_id": entry_id, "search_text": search_text},
                )
            prepared.append((ref, values, term_rows))
        try:
            async with self.artifacts.hold([ref for ref, _, _ in prepared]), self.db.locked() as session:
                await self._staging(session, identity)
                from .catalog_sql_budget import check_sql_budget
                estimated = sum(1024 + len(strict_json_dumps(values["summary"]).encode())
                                + len(values["search_text"].encode()) + len(values["original_name"].encode())
                                for _, values, _ in prepared)
                await check_sql_budget(session, estimated, retiring=True)
                pending_terms = []
                for ref, values, term_rows in prepared:
                    if await session.get(ArtifactObject, ref.id) is None:
                        session.add(ArtifactObject(id=ref.id, size_bytes=ref.size_bytes, sha256=ref.sha256))
                        await session.flush()
                    session.add(CatalogEntry(**values))
                    pending_terms.extend(term_rows)
                generation, _, _ = await self._staging(session, identity)
                generation.pending_pages += 1
                await session.flush()
            from .catalog_terms import insert_term_rows

            for offset in range(0, len(pending_terms), 5000):
                async with self.db.locked() as session:
                    await self._staging(session, identity)
                    await check_sql_budget(session, len(pending_terms[offset:offset + 5000]) * 1024)
                    await insert_term_rows(session, pending_terms[offset:offset + 5000])
            async with self.db.locked() as session:
                generation, _, _ = await self._staging(session, identity)
                generation.pending_pages -= 1
        except IntegrityError:
            raise GatewayError("catalog_duplicate_entry", "Catalog contains duplicate entries or conflicting hashes") from None

    async def set_metadata(self, identity, metadata):
        summary = await asyncio.to_thread(metadata_summary, metadata)
        async with self.db.locked() as session:
            generation, _, _ = await self._staging(session, identity)
            generation.details = summary

    async def discard_kind(self, identity, kind):
        self._check_kind(kind)
        while True:
            async with self.db.locked() as session:
                await self._staging(session, identity)
                ids = list((await session.scalars(select(CatalogEntry.id).where(
                    CatalogEntry.generation_id == identity, CatalogEntry.kind == kind
                ).limit(250))).all())
                if not ids:
                    return
                await session.execute(delete(CatalogEntry).where(CatalogEntry.id.in_(ids)))

    async def publish(self, identity, *, expected_scope):
        # Freeze staging before the bounded streaming digest pass, outside the final publish transaction.
        async with self.db.locked() as session:
            generation, head, _ = await self._staging(session, identity)
            errors = (generation.details or {}).get("capability_errors") or []
            if errors and head.generation_id:
                previous = await session.get(CatalogGeneration, head.generation_id)
                lost = [item.get("capability") for item in errors if isinstance(item, dict)
                        and (previous.counts or {}).get(item.get("capability"), 0)] if previous else []
                if previous and generation_scope(previous) == expected_scope and lost:
                    raise GatewayError("discovery_failed", "Capability refresh failed for: " + ", ".join(lost))
            if generation_scope(generation) != expected_scope:
                raise GatewayError("credential_changed", "Catalog refresh does not match the current scope")
            if generation.pending_pages:
                raise GatewayError("catalog_incomplete", "Catalog contains an incomplete staging page")
            generation.status = "publishing"
        counts = dict.fromkeys(sorted(KINDS), 0)
        accumulator = hashlib.sha256()
        preview, preview_bytes, descriptions = [], 0, []
        previous = None
        while True:
            statement = select(
                CatalogEntry.kind, CatalogEntry.name_hash, CatalogEntry.artifact_id, CatalogEntry.summary
            ).where(CatalogEntry.generation_id == identity)
            if previous is not None:
                statement = statement.where(or_(
                    CatalogEntry.kind > previous[0],
                    and_(CatalogEntry.kind == previous[0], CatalogEntry.name_hash > previous[1])))
            async with self.db.session() as session:
                rows = (await session.execute(statement.order_by(
                    CatalogEntry.kind, CatalogEntry.name_hash).limit(250))).all()
            if not rows:
                break
            previous = (rows[-1].kind, rows[-1].name_hash)
            for kind, name_hash, artifact_id, summary in rows:
                counts[kind] += 1
                accumulator.update(strict_json_dumps([kind, name_hash, artifact_id]).encode())
                if kind == "tools":
                    name = summary["name"]
                    cost = len(strict_json_dumps(name).encode()) + 1
                    if len(preview) < 50 and preview_bytes + cost <= 4096:
                        preview.append(name)
                        preview_bytes += cost
                    description = summary.get("description", "").strip()
                    if description and len(descriptions) < 3 and description not in descriptions:
                        descriptions.append(description)
            await asyncio.sleep(0)
        generated = ("Provides " + str(counts["tools"]) + " tools: " + (
            "; ".join(descriptions) if descriptions else ", ".join(sorted(preview)[:6])
        ))[:480] if preview or descriptions else ""
        from .catalog_manifests import build_manifest
        await build_manifest(self, generation, counts, accumulator.hexdigest())
        async with self.db.locked() as session:
            generation = await session.get(CatalogGeneration, identity, populate_existing=True)
            head = await session.get(CatalogHead, scope_key(expected_scope), populate_existing=True)
            await self._validate_scope(session, expected_scope)
            if (
                not generation or not head or head.refresh_id != identity or generation.status != "publishing"
                or generation_scope(generation) != expected_scope
            ):
                raise GatewayError("generation_superseded", "Catalog changed before publication")
            generation.counts, generation.digest = counts, accumulator.hexdigest()
            generation.details = dict(generation.details or {}, tools_preview=sorted(preview),
                                      generated_description=generated)
            from .embeddings import reuse_generation_vectors

            await reuse_generation_vectors(session, head.generation_id, generation)
            generation.status = "pending_control"
            head.pending_id = identity
            await session.flush()
        async with self.authority.locked() as primary:
            await self._validate_scope(primary, expected_scope)
            control = await self._control(primary, expected_scope)
            refresh = (control.receipt or {}).get("refresh", {}) if control else {}
            if (not control or control.epoch != generation.control_epoch
                    or control.refresh_fence != generation.refresh_fence
                    or refresh.get("ticket") != generation.refresh_ticket
                    or refresh.get("expires_at", 0) <= time.time()):
                raise GatewayError("generation_superseded", "Refresh ticket was superseded before receipt commit")
            receipt = dict(control.receipt or {})
            if refresh.get("state") == "issued":
                from .catalog_operations import current_operation
                operation = current_operation.get()
                receipt["refresh"] = dict(refresh, state="committed",
                                          operation_id=operation.identity if operation else None)
                control.health = {}
                control.directory_sequence += 1
                control.success_sequence += 1
                control.receipt, control.updated_at = receipt, now()
                if operation:
                    operation.success_version = control.success_sequence
                    if operation.ticket:
                        operation.ticket.update(success=control.success_sequence, fence=control.refresh_fence,
                                                epoch=control.epoch)
        await self._materialize(identity, expected_scope)
        async with self.db.session() as session:
            result = generation_ref(await session.get(CatalogGeneration, identity))
        if self.on_publish:
            await self.on_publish(generation.server_id)
        return result

    async def _current(self, session, scope):
        # The authoritative pair shares one primary snapshot; the head and
        # generation share one derived snapshot. Never join across databases.
        pair = (await session.execute(select(McpServer, CatalogControl).outerjoin(
            CatalogControl, CatalogControl.scope_key == scope_key(scope)
        ).options(load_only(
            McpServer.id, McpServer.slug, McpServer.revision, McpServer.isolation, McpServer.mode
        )).where(McpServer.id == scope.server_id).execution_options(populate_existing=True))).first()
        if pair is None:
            return None
        row, control = pair
        if row.revision != scope.revision or row.mode == "disabled":
            return None
        if control and (control.server_id, control.owner) != (scope.server_id, scope.owner):
            raise GatewayError("catalog_hash_collision", "Catalog scope hash collision")
        if not control or (control.revision, control.credential_generation) != (
                scope.revision, scope.credential_generation):
            return None
        pair = (await session.execute(select(CatalogHead, CatalogGeneration).join(
            CatalogGeneration, CatalogGeneration.id == CatalogHead.generation_id
        ).where(CatalogHead.scope_key == scope_key(scope)).execution_options(populate_existing=True))).first()
        if pair is None:
            return None
        head, generation = pair
        if (head.owner != scope.owner or head.server_id != scope.server_id or head.status != "ready"
                or generation.control_epoch != control.epoch or generation.status != "ready"
                or generation_scope(generation) != scope):
            return None
        if self.scope_resolver and await self.scope_resolver(session, row, scope.owner) != scope:
            return None
        return generation

    async def query_tool_entries(self, generations, *, kind="tools", query="", exact_names=None, offset=0, limit=20,
                                 uri_prefix="", display_name=None):
        """Filter/count/page lightweight rows in SQL before any contract or artifact read."""
        self._check_kind(kind)
        if query and exact_names is None and kind == "tools":
            from .catalog_retrieval import MAX_CANDIDATES, search_candidates

            cache_key = (tuple(sorted(generations)), query.casefold().strip())
            async with self._search_cache_lock:
                candidates = self._search_cache.get(cache_key)
                if candidates is not None:
                    self._search_cache.move_to_end(cache_key)
            if candidates is None:
                candidates = tuple(await search_candidates(
                    self.db, list(generations), query, limit=MAX_CANDIDATES
                ))
                cost = sum(len(row.search_text.encode()) + len(strict_json_dumps(row.summary).encode()) + 512
                           for row in candidates) + sum(len(identity) for identity in generations) + len(query.encode())
                if cost <= self._search_cache_max_bytes:
                    async with self._search_cache_lock:
                        self._search_cache_bytes -= self._search_sizes.pop(cache_key, 0)
                        self._search_cache[cache_key] = candidates
                        self._search_sizes[cache_key] = cost
                        self._search_cache_bytes += cost
                        self._search_cache.move_to_end(cache_key)
                        while (len(self._search_cache) > self._search_cache_entries
                               or self._search_cache_bytes > self._search_cache_max_bytes):
                            removed, _ = self._search_cache.popitem(last=False)
                            self._search_cache_bytes -= self._search_sizes.pop(removed)
            total = len(candidates)
            return total, list(candidates[offset:offset + limit]) if limit else []
        # Exact name hits are selective. Keep authorization as a residual
        # membership check so SQLite cannot enumerate every allowed generation
        # and scan all its tools before applying the exact-name hit set.
        generation_column = cast(CatalogEntry.generation_id, String) if exact_names is not None else CatalogEntry.generation_id
        filters = [generation_column.in_(select(ALLOWED.c.id)), CatalogEntry.kind == kind]
        if uri_prefix:
            filters.append(func.hex(CatalogEntry.original_name).startswith(uri_prefix.encode("utf-8").hex().upper()))
        if display_name is not None:
            filters.append(func.hex(CatalogEntry.summary["name"].as_string()) == display_name.encode("utf-8").hex().upper())
        if exact_names is not None:
            hashes = [digest(name) for name in exact_names]
            matches = union(
                select(CatalogEntry.id).where(CatalogEntry.name_hash.in_(hashes)),
                select(CatalogEntry.id).where(CatalogEntry.gateway_hash.in_(hashes)),
                select(CatalogEntry.id).where(CatalogEntry.legacy_gateway_hash.in_(hashes)),
            ).subquery()
            # A global miss contains no authorized result. Avoid constructing
            # thousands of allowed-scope rows just to prove that zero remains zero.
            async with self.db.session() as probe:
                hit_generations = list((await probe.scalars(select(CatalogEntry.generation_id).where(
                    CatalogEntry.kind == kind, CatalogEntry.id.in_(select(matches.c.id))).limit(251))).all())
            if not hit_generations:
                return 0, []
            if len(hit_generations) <= 250:
                # This bounded probe contains every hit. Unique names need
                # only their authorized generation, rather than staging the
                # entire directory. Larger hit sets keep the general SQL path.
                hits = set(hit_generations)
                generations = [identity for identity in generations if identity in hits]
                if not generations:
                    return 0, []
            filters.append(CatalogEntry.id.in_(select(matches.c.id)))
        elif query:
            pattern = "%" + query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            filters.append(CatalogEntry.search_text.like(pattern, escape="\\"))
        async with query_generations(self.db, generations) as session:
            total = await session.scalar(select(func.count()).select_from(CatalogEntry).where(*filters))
            rows = (await session.execute(select(
                CatalogEntry.id, CatalogEntry.generation_id, CatalogGeneration.server_id,
                CatalogEntry.original_name,
                CatalogEntry.gateway_name, CatalogEntry.schema_hash, CatalogEntry.artifact_id,
                CatalogEntry.artifact_bytes, CatalogEntry.summary, CatalogEntry.search_text,
            ).join(CatalogGeneration, CatalogGeneration.id == CatalogEntry.generation_id).join(
                CatalogServer, CatalogServer.id == CatalogGeneration.server_id
            ).where(*filters).order_by(
                CatalogServer.slug, CatalogEntry.sort_key, CatalogEntry.name_hash
            ).offset(offset).limit(limit))).all()
            return total, rows

    async def has_scope(self, scope):
        async with self.db.session() as session:
            return await session.get(CatalogHead, scope_key(scope)) is not None

    async def get_summary(self, scope):
        await self._recover_scope(scope)
        async with self.db.session() as session:
            generation = await self._current(session, scope)
            counts = generation.counts if generation else {}
            return {
                "generation": generation.id if generation else None,
                "catalog_version": generation.digest if generation else "",
                "status": "ready" if generation else "empty",
                "tool_count": counts.get("tools", 0), "resource_count": counts.get("resources", 0),
                "prompt_count": counts.get("prompts", 0), "template_count": counts.get("templates", 0),
                **(generation.details if generation else {}),
            }

    async def get_summaries(self, scopes, *, preview=False):
        """Bounded authority/control reads followed by derived-only metadata queries."""
        expected = {scope_key(scope): scope for scope in scopes}
        if not expected:
            return {}
        controls, servers, rows = {}, {}, []
        keys = list(expected)
        for offset in range(0, len(keys), 250):
            batch = keys[offset:offset + 250]
            async with self.db.session() as session:
                pending = (await session.execute(select(CatalogHead.scope_key, CatalogHead.pending_id).where(
                    CatalogHead.scope_key.in_(batch), CatalogHead.pending_id.is_not(None)))).all()
            for key, identity in pending:
                await self._materialize(identity, expected[key])
            async with self.authority.session() as primary:
                controls.update({row.scope_key: row for row in (await primary.execute(select(
                    CatalogControl.scope_key, CatalogControl.revision, CatalogControl.credential_generation,
                    CatalogControl.epoch, CatalogControl.health
                ).where(CatalogControl.scope_key.in_(batch)))).all()})
                ids = list({expected[key].server_id for key in batch})
                servers.update({row.id: row for row in (await primary.execute(select(
                    McpServer.id, McpServer.revision, McpServer.mode).where(
                        McpServer.id == ids[0] if len(ids) == 1 else McpServer.id.in_(ids)))).all()})
            async with self.db.session() as session:
                rows.extend((await session.execute(select(
                    CatalogHead.scope_key, CatalogHead.server_id, CatalogHead.owner, CatalogHead.status,
                    CatalogGeneration.id.label("generation_id"), CatalogGeneration.status.label("generation_status"),
                    CatalogGeneration.revision, CatalogGeneration.credential_generation,
                    CatalogGeneration.control_epoch, CatalogGeneration.counts,
                    CatalogGeneration.published_at, CatalogGeneration.details,
                ).select_from(CatalogHead).outerjoin(CatalogGeneration,
                    CatalogHead.generation_id == CatalogGeneration.id
                ).where(CatalogHead.scope_key.in_(batch)))).all())
        result = {}
        for row in rows:
            scope = expected[row.scope_key]
            control, server = controls.get(row.scope_key), servers.get(row.server_id)
            valid = bool(control and server and server.mode != "disabled" and server.revision == scope.revision
                         and (control.revision, control.credential_generation) == (
                             scope.revision, scope.credential_generation))
            ready = bool(valid and row.status == "ready" and row.generation_status == "ready"
                         and row.control_epoch == control.epoch
                         and CatalogScope(row.server_id, row.owner, row.revision,
                                          row.credential_generation) == scope)
            health = control.health if valid else {}
            counts = (row.counts or {}) if ready else {}
            item = {
                "generation": row.generation_id if ready else None,
                "control_epoch": control.epoch if valid else None,
                "cache_status": "ready" if ready else "error" if health.get("invalidated") or row.status == "error" else "empty",
                "cache_at": row.published_at if ready else None,
                **{key[:-1] + "_count": counts.get(key, 0) for key in KINDS},
                **{key: value for key, value in health.items() if key != "invalidated"},
            }
            if preview:
                details = (row.details or {}) if ready else {}
                item.update(tools_preview=details.get("tools_preview", []),
                            generated_description=details.get("generated_description", ""))
            result[row.server_id] = item
        for key, scope in expected.items():
            if scope.server_id in result:
                continue
            control = controls.get(key)
            server = servers.get(scope.server_id)
            if (control and server and server.revision == scope.revision
                    and (control.revision, control.credential_generation) == (
                        scope.revision, scope.credential_generation)):
                health = dict(control.health or {})
                result[scope.server_id] = {
                    "generation": None, "control_epoch": control.epoch,
                    "cache_status": "error" if health else "empty",
                    "cache_at": None, **{kind[:-1] + "_count": 0 for kind in KINDS},
                    **({"tools_preview": [], "generated_description": ""} if preview else {}),
                    **{name: value for name, value in health.items() if name not in {"invalidated", "failure_sequence"}},
                }
        return result

    def _cursor(self, payload):
        raw = strict_json_dumps(payload).encode()
        signature = hmac.new(self.signing_key, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")

    def _decode_cursor(self, value, binding):
        try:
            if not isinstance(value, str) or len(value) > 4096:
                raise ValueError
            packed = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            raw, signature = packed[:-32], packed[-32:]
            if not hmac.compare_digest(signature, hmac.new(self.signing_key, raw, hashlib.sha256).digest()):
                raise ValueError
            payload = json.loads(raw)
            if payload["binding"] != binding or not isinstance(payload["key"], list) or len(payload["key"]) != 2:
                raise ValueError
            return payload["key"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise GatewayError("invalid_cursor", "Cursor is invalid or its query/scope changed") from None

    async def list_capabilities(self, scope, *, kind, query="", cursor=None, limit=50):
        await self._recover_scope(scope)
        self._check_kind(kind)
        if not isinstance(limit, int) or not 1 <= limit <= 200 or not isinstance(query, str):
            raise ValueError("Invalid capability query or page size")
        query = query.casefold().strip()
        async with self.db.session() as session:
            generation = await self._current(session, scope)
            if not generation:
                if cursor:
                    raise GatewayError("invalid_cursor", "Catalog scope or generation changed")
                return CatalogPage([], 0, None, "")
            binding = digest(strict_json_dumps([scope_key(scope), generation.id, scope.revision,
                                               scope.credential_generation, kind, query, limit]))
            key = self._decode_cursor(cursor, binding) if cursor else None
            filters = [CatalogEntry.generation_id == generation.id, CatalogEntry.kind == kind]
            if query:
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                filters.append(CatalogEntry.search_text.like("%" + escaped + "%", escape="\\"))
            total = await session.scalar(select(func.count()).select_from(CatalogEntry).where(*filters))
            statement = select(CatalogEntry.summary, CatalogEntry.sort_key, CatalogEntry.name_hash).where(*filters)
            if key:
                statement = statement.where(or_(
                    CatalogEntry.sort_key > key[0],
                    and_(CatalogEntry.sort_key == key[0], CatalogEntry.name_hash > key[1]),
                ))
            rows = (await session.execute(statement.order_by(
                CatalogEntry.sort_key, CatalogEntry.name_hash
            ).limit(limit + 1))).all()
            selected = await bounded_summary_rows(rows, limit)
            next_cursor = self._cursor({"binding": binding, "key": [selected[-1].sort_key, selected[-1].name_hash]}) if len(rows) > len(selected) else None
            return CatalogPage([dict(row.summary) for row in selected], total, next_cursor, generation.id)

    async def _entry(self, session, scope, kind, name):
        generation = await self._current(session, scope)
        if not generation:
            return None
        entry = await session.scalar(select(CatalogEntry).where(
            CatalogEntry.generation_id == generation.id, CatalogEntry.kind == kind, CatalogEntry.name_hash == digest(name)
        ))
        if entry and entry.original_name != name:
            raise GatewayError("catalog_hash_collision", "Capability name hash collision")
        return entry

    async def has_capability(self, scope, *, kind, name):
        await self._recover_scope(scope)
        """Exact metadata-only membership check; never read a definition artifact."""
        self._check_kind(kind)
        async with self.db.session() as session:
            return await self._entry(session, scope, kind, name) is not None

    async def get_tool(self, scope, original_name):
        await self._recover_scope(scope)
        async with self.db.session() as session:
            entry = await self._entry(session, scope, "tools", original_name)
            return tool_ref(entry) if entry else None

    async def get_definition(self, scope, *, kind, name):
        await self._recover_scope(scope)
        self._check_kind(kind)
        async with self.db.session() as session:
            entry = await self._entry(session, scope, kind, name)
            if not entry:
                return None
            ref = ArtifactRef(entry.artifact_id, entry.artifact_bytes, entry.artifact_id)
        value = await self.artifacts.read_json(ref, max_bytes=MAX_DEFINITION_BYTES)
        # Never serve a definition after a slow read if its scope/head was replaced.
        async with self.db.session() as session:
            current = await self._entry(session, scope, kind, name)
            if not current or current.generation_id != entry.generation_id:
                raise GatewayError("revision_changed", "Catalog changed while reading the definition")
        return value

    async def resolve_gateway_name(self, gateway_name, allowed_ids, *, owner="service", kind="tools"):
        self._check_kind(kind)
        if not allowed_ids:
            return None
        name_hash = digest(gateway_name)
        async with self.db.session() as session:
            statement = select(CatalogEntry, CatalogGeneration, CatalogServer).join(
                CatalogGeneration, CatalogGeneration.id == CatalogEntry.generation_id
            ).join(CatalogHead, CatalogHead.generation_id == CatalogGeneration.id).join(
                CatalogServer, CatalogServer.id == CatalogGeneration.server_id
            ).where(
                CatalogServer.id.in_(allowed_ids), CatalogServer.mode != "disabled",
                CatalogServer.revision == CatalogGeneration.revision,
                CatalogGeneration.status == "ready", CatalogHead.status == "ready",
                CatalogEntry.kind == kind,
                or_(CatalogEntry.gateway_hash == name_hash, CatalogEntry.legacy_gateway_hash == name_hash),
            )
            matches = []
            for entry, generation, projection in (await session.execute(statement)).all():
                try:
                    server = await self._validate_scope(session, generation_scope(generation))
                except GatewayError:
                    continue
                control = await self._control(session, generation_scope(generation))
                if not control or generation.control_epoch != control.epoch:
                    continue
                expected_owner = owner if server.isolation == "user" else "service"
                if generation.owner != expected_owner:
                    continue
                if gateway_name not in {entry.gateway_name, server.id + "__" + entry.original_name}:
                    raise GatewayError("catalog_hash_collision", "Gateway name hash collision")
                if self.scope_resolver and await self.scope_resolver(session, server, expected_owner) != generation_scope(generation):
                    continue
                matches.append(tool_ref(entry))
            if len(matches) > 1:
                raise GatewayError("catalog_route_conflict", "Gateway name resolves to multiple authorized tools")
            return matches[0] if matches else None

    async def get_generation_scope(self, identity):
        async with self.db.session() as session:
            generation = await session.get(CatalogGeneration, identity)
            return generation_scope(generation) if generation else None

    async def refresh_version(self, scope):
        async with self.db.session() as session:
            head = await session.get(CatalogHead, scope_key(scope))
            return head.refresh_id if head else None

    async def invalidate_authorization(self, scope, details):
        """Fence prior definitions and in-flight results after an explicit grant change."""
        async with self.authority.locked() as primary:
            await self._validate_scope(primary, scope)
            control = await self._control(primary, scope, create=True)
            control.epoch += 1
            control.refresh_fence += 1
            control.receipt = {}
            health = dict(control.health or {})
            health.update(details, invalidated=True, cache_stale=False)
            control.health, control.updated_at = health, now()
        return health

    async def project_failure(self, scope, identity, details):
        """Project one accepted diagnostic; never turn a published generation into a failure."""
        async with self.db.locked() as session:
            generation = await session.get(CatalogGeneration, identity) if identity else None
            if (not generation or generation_scope(generation) != scope
                    or generation.status not in {"staging", "publishing", "failed"}):
                # Connection failures precede generation creation. Reuse one
                # disposable diagnostic row without mutating the last good head.
                generation = await session.scalar(select(CatalogGeneration).where(
                    CatalogGeneration.scope_key == scope_key(scope),
                    CatalogGeneration.status == "failed").order_by(
                    CatalogGeneration.created_at.desc()).limit(1))
                if generation is None:
                    if await session.get(CatalogServer, scope.server_id) is None:
                        return  # Authoritative health remains available without a projection.
                    generation = CatalogGeneration(
                        scope_key=scope_key(scope), server_id=scope.server_id, owner=scope.owner,
                        revision=scope.revision, credential_generation=scope.credential_generation,
                        status="failed", details={})
                    session.add(generation)
            generation.status = "failed"
            generation.details = dict(generation.details or {}) | details

    async def fail_refresh(self, identity):
        async with self.db.locked() as session:
            generation = await session.get(CatalogGeneration, identity)
            if not generation:
                return
            control = await self._control(session, generation_scope(generation))
            if self._matches_receipt(control, generation):
                return  # Committed success is recovered, never converted into a failed operation.
            generation.status = "failed"
            head = await session.get(CatalogHead, generation.scope_key)
            if head and head.refresh_id == identity:
                previous = await session.get(CatalogGeneration, head.generation_id) if head.generation_id else None
                head.status = "ready" if previous and previous.status == "ready" else "error"

    async def rebind_renewed_credential(self, session, before, after):
        """Carry only this grant's last good contracts across renewal, never across accounts."""
        if (before.server_id, before.owner, before.revision) != (after.server_id, after.owner, after.revision):
            raise GatewayError("credential_changed", "Cannot transfer a catalog between credential owners")
        control = await self._control(session, before)
        if not control:
            return
        control.credential_generation = after.credential_generation
        control.refresh_fence += 1
        receipt = dict(control.receipt or {})
        receipt.pop("refresh", None)
        control.receipt = receipt
        async with self.db.locked() as derived:
            head = await derived.get(CatalogHead, scope_key(before))
            generation = await derived.get(CatalogGeneration, head.generation_id) if head and head.generation_id else None
            if (head and head.status == "ready" and generation and generation.status == "ready"
                    and generation_scope(generation) == before and generation.control_epoch == control.epoch):
                generation.credential_generation = after.credential_generation
        from .catalog_operations import current_operation
        operation = current_operation.get()
        if operation and operation.scope == before and operation.ticket:
            operation.ticket.update(epoch=control.epoch, fence=control.refresh_fence,
                                    success=control.success_sequence)


    async def detach_server(self, server_id):
        """Retire cache roots after authoritative deletion; never cascade a large index."""
        from .catalog_models import CatalogManifest
        async with self.db.locked() as session:
            heads = (await session.scalars(select(CatalogHead).where(CatalogHead.server_id == server_id))).all()
            for head in heads:
                head.generation_id, head.refresh_id, head.pending_id, head.status = None, None, None, "error"
            for manifest in (await session.scalars(select(CatalogManifest).where(
                    CatalogManifest.scope_key.in_([head.scope_key for head in heads]),
                    CatalogManifest.current.is_(True)))).all():
                manifest.current, manifest.retire_after = False, now()
