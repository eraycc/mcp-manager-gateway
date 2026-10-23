"""Portable, paged catalog reads and short atomic generation publication."""
import asyncio
import base64
import hashlib
import hmac
import json
from collections import OrderedDict

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only

from .artifact_store import ArtifactStore
from .catalog_models import ArtifactObject, CatalogEntry, CatalogGeneration, CatalogHead, CatalogTerm
from .catalog_types import ArtifactRef, CatalogPage, CatalogScope, GenerationRef, ToolRef
from .database import McpServer, now, uid
from .db_types import strict_json_dumps
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
        self.db = db
        self.artifacts = artifacts
        self.signing_key = signing_key.encode() if isinstance(signing_key, str) else signing_key
        self.scope_resolver = scope_resolver
        self.on_publish = None
        self._search_cache = OrderedDict()
        self._search_cache_lock = asyncio.Lock()
        self._search_cache_entries = 64

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

    async def begin_refresh(self, scope):
        if not isinstance(scope, CatalogScope) or not scope.owner or len(scope.credential_generation) > 128:
            raise ValueError("Invalid catalog scope")
        async with self.db.locked() as session:
            await self._validate_scope(session, scope)
            key = scope_key(scope)
            head = await session.get(CatalogHead, key)
            if head and (head.server_id != scope.server_id or head.owner != scope.owner):
                raise GatewayError("catalog_hash_collision", "Catalog scope hash collision")
            generation = CatalogGeneration(
                scope_key=key, server_id=scope.server_id, owner=scope.owner, revision=scope.revision,
                credential_generation=scope.credential_generation, status="staging", digest="", counts={},
            )
            session.add(generation)
            await session.flush()
            if head is None:
                head = CatalogHead(scope_key=key, server_id=scope.server_id, owner=scope.owner, status="empty")
                session.add(head)
            head.refresh_id = generation.id
            await session.flush()
            result = generation_ref(generation)
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
            async with self.db.locked() as session:
                await self._staging(session, identity)
                pending_terms = []
                for ref, values, term_rows in prepared:
                    if await session.get(ArtifactObject, ref.id) is None:
                        session.add(ArtifactObject(id=ref.id, size_bytes=ref.size_bytes, sha256=ref.sha256))
                        await session.flush()
                    session.add(CatalogEntry(**values))
                    pending_terms.extend(term_rows)
                await session.flush()
                session.add_all(CatalogTerm(**values) for values in pending_terms)
                await session.flush()
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
            generation, _, _ = await self._staging(session, identity)
            if generation_scope(generation) != expected_scope:
                raise GatewayError("credential_changed", "Catalog refresh does not match the current scope")
            generation.status = "publishing"
        counts = dict.fromkeys(sorted(KINDS), 0)
        accumulator = hashlib.sha256()
        preview, preview_bytes, descriptions = [], 0, []
        async with self.db.session() as session:
            stream = await session.stream(select(
                CatalogEntry.kind, CatalogEntry.name_hash, CatalogEntry.artifact_id, CatalogEntry.summary
            ).where(CatalogEntry.generation_id == identity).order_by(
                CatalogEntry.kind, CatalogEntry.name_hash
            ).execution_options(yield_per=250))
            async for kind, name_hash, artifact_id, summary in stream:
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
            generated = ("Provides " + str(counts["tools"]) + " tools: " + (
                "; ".join(descriptions) if descriptions else ", ".join(sorted(preview)[:6])
            ))[:480] if preview or descriptions else ""
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
            from .embedding_models import EmbeddingVector
            from .embeddings import reuse_generation_vectors

            await reuse_generation_vectors(session, head.generation_id, generation)
            generation.status, generation.published_at = "ready", now()
            head.generation_id, head.status = identity, "ready"
            # The new head owns all current vectors; removed/changed tools leave no stale index rows.
            await session.execute(delete(EmbeddingVector).where(
                EmbeddingVector.scope_key == generation.scope_key,
                EmbeddingVector.generation_id != identity,
            ))
            await session.flush()
            result = generation_ref(generation)
        if self.on_publish:
            await self.on_publish(generation.server_id)
        return result

    async def _current(self, session, scope):
        row = await session.get(McpServer, scope.server_id, populate_existing=True, options=[load_only(
            McpServer.id, McpServer.slug, McpServer.revision, McpServer.isolation, McpServer.mode
        )])
        if not row or row.revision != scope.revision or row.mode == "disabled":
            return None
        head = await session.get(CatalogHead, scope_key(scope), populate_existing=True)
        if not head or head.owner != scope.owner or head.server_id != scope.server_id or head.status != "ready":
            return None
        generation = await session.get(CatalogGeneration, head.generation_id, populate_existing=True) if head.generation_id else None
        if not generation or generation.status != "ready" or generation_scope(generation) != scope:
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
                async with self._search_cache_lock:
                    self._search_cache[cache_key] = candidates
                    self._search_cache.move_to_end(cache_key)
                    while len(self._search_cache) > self._search_cache_entries:
                        self._search_cache.popitem(last=False)
            total = len(candidates)
            return total, list(candidates[offset:offset + limit]) if limit else []
        filters = [CatalogEntry.generation_id.in_(generations), CatalogEntry.kind == kind]
        if uri_prefix:
            filters.append(func.hex(CatalogEntry.original_name).startswith(uri_prefix.encode("utf-8").hex().upper()))
        if display_name is not None:
            filters.append(func.hex(CatalogEntry.summary["name"].as_string()) == display_name.encode("utf-8").hex().upper())
        if exact_names is not None:
            hashes = [digest(name) for name in exact_names]
            filters.append(or_(
                CatalogEntry.name_hash.in_(hashes), CatalogEntry.gateway_hash.in_(hashes),
                CatalogEntry.legacy_gateway_hash.in_(hashes),
            ))
        elif query:
            pattern = "%" + query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            filters.append(CatalogEntry.search_text.like(pattern, escape="\\"))
        async with self.db.session() as session:
            total = await session.scalar(select(func.count()).select_from(CatalogEntry).where(*filters))
            rows = (await session.execute(select(
                CatalogEntry.id, CatalogEntry.generation_id, CatalogGeneration.server_id,
                CatalogEntry.original_name,
                CatalogEntry.gateway_name, CatalogEntry.schema_hash, CatalogEntry.artifact_id,
                CatalogEntry.artifact_bytes, CatalogEntry.summary, CatalogEntry.search_text,
            ).join(CatalogGeneration, CatalogGeneration.id == CatalogEntry.generation_id).join(
                McpServer, McpServer.id == CatalogGeneration.server_id
            ).where(*filters).order_by(
                McpServer.slug, CatalogEntry.sort_key, CatalogEntry.name_hash
            ).offset(offset).limit(limit))).all()
            return total, rows

    async def has_scope(self, scope):
        async with self.db.session() as session:
            return await session.get(CatalogHead, scope_key(scope)) is not None

    async def get_summary(self, scope):
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
        """One projected metadata query; failed/stale heads never fall back to old caches."""
        if not scopes:
            return {}
        expected = {scope_key(scope): scope for scope in scopes}
        columns = [
            CatalogHead.scope_key, CatalogHead.server_id, CatalogHead.owner, CatalogHead.status,
            CatalogGeneration.id.label("generation_id"), CatalogGeneration.status.label("generation_status"),
            CatalogGeneration.revision, CatalogGeneration.credential_generation,
            CatalogGeneration.counts, CatalogGeneration.published_at, CatalogGeneration.details,
        ]
        async with self.db.session() as session:
            rows = (await session.execute(select(*columns).select_from(CatalogHead).outerjoin(
                CatalogGeneration, CatalogHead.generation_id == CatalogGeneration.id
            ).where(CatalogHead.scope_key.in_(expected)))).all()
        result = {}
        for row in rows:
            scope = expected[row.scope_key]
            ready = (row.status == "ready" and row.generation_status == "ready"
                     and CatalogScope(row.server_id, row.owner, row.revision, row.credential_generation) == scope)
            counts = (row.counts or {}) if ready else {}
            item = {
                "generation": row.generation_id if ready else None,
                "cache_status": "ready" if ready else "error" if row.status == "error" else "empty",
                "cache_at": row.published_at if ready else None,
                "tool_count": counts.get("tools", 0), "resource_count": counts.get("resources", 0),
                "prompt_count": counts.get("prompts", 0), "template_count": counts.get("templates", 0),
            }
            failure = (row.status == "error" and row.revision == scope.revision
                       and row.credential_generation == scope.credential_generation)
            if failure:
                item.update({key: value for key, value in (row.details or {}).items() if key in {
                    "cache_error", "cache_error_code", "last_refresh_error", "last_refresh_error_code",
                    "last_refresh_failure_at", "cache_attempt_at",
                }})
            if preview:
                details = (row.details or {}) if ready else {}
                item.update(tools_preview=details.get("tools_preview", []),
                            generated_description=details.get("generated_description", ""))
            result[row.server_id] = item
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
        """Exact metadata-only membership check; never read a definition artifact."""
        self._check_kind(kind)
        async with self.db.session() as session:
            return await self._entry(session, scope, kind, name) is not None

    async def get_tool(self, scope, original_name):
        async with self.db.session() as session:
            entry = await self._entry(session, scope, "tools", original_name)
            return tool_ref(entry) if entry else None

    async def get_definition(self, scope, *, kind, name):
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
            statement = select(CatalogEntry, CatalogGeneration, McpServer).options(load_only(
                McpServer.id, McpServer.slug, McpServer.revision, McpServer.mode, McpServer.isolation
            )).join(
                CatalogGeneration, CatalogGeneration.id == CatalogEntry.generation_id
            ).join(CatalogHead, CatalogHead.generation_id == CatalogGeneration.id).join(
                McpServer, McpServer.id == CatalogGeneration.server_id
            ).where(
                McpServer.id.in_(allowed_ids), McpServer.mode != "disabled",
                McpServer.revision == CatalogGeneration.revision,
                CatalogGeneration.status == "ready", CatalogHead.status == "ready",
                CatalogEntry.kind == kind,
                or_(CatalogEntry.gateway_hash == name_hash, CatalogEntry.legacy_gateway_hash == name_hash),
            )
            matches = []
            for entry, generation, server in (await session.execute(statement)).all():
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

    async def record_failure(self, scope, details, *, expected_refresh):
        """Invalidate only this scope; a late failure cannot replace newer success."""
        async with self.db.locked() as session:
            await self._validate_scope(session, scope)
            key = scope_key(scope)
            head = await session.get(CatalogHead, key)
            failed = await session.get(CatalogGeneration, head.refresh_id) if head and head.refresh_id else None
            if (head and head.refresh_id != expected_refresh
                    and (head.status != "error" or not failed or failed.status != "failed")):
                return False
            if failed is None or failed.status != "failed" or generation_scope(failed) != scope:
                failed = CatalogGeneration(
                    scope_key=key, server_id=scope.server_id, owner=scope.owner, revision=scope.revision,
                    credential_generation=scope.credential_generation, status="failed", digest="", counts={},
                )
                session.add(failed)
                await session.flush()
            failed.details = dict(details)
            if head is None:
                head = CatalogHead(scope_key=key, server_id=scope.server_id, owner=scope.owner)
                session.add(head)
            head.status, head.generation_id, head.refresh_id = "error", failed.id, failed.id
            return True

    async def fail_refresh(self, identity):
        async with self.db.locked() as session:
            generation = await session.get(CatalogGeneration, identity)
            if not generation:
                return
            generation.status = "failed"
            head = await session.get(CatalogHead, generation.scope_key)
            if head and head.refresh_id == identity:
                head.status = "error"
