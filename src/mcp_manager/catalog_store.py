"""Portable, paged catalog reads and short atomic generation publication."""
import asyncio
import base64
import hashlib
import hmac
import json

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from .artifact_store import ArtifactStore
from .catalog_models import ArtifactObject, CatalogEntry, CatalogGeneration, CatalogHead
from .catalog_types import ArtifactRef, CatalogPage, CatalogScope, GenerationRef, ToolRef
from .database import McpServer, now
from .db_types import strict_json_dumps
from .runtime import GatewayError

KINDS = {"tools", "resources", "prompts", "templates"}
MAX_DEFINITION_BYTES = 8 * 1024 * 1024


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


class CatalogStore:
    def __init__(self, db, artifacts: ArtifactStore, *, signing_key, scope_resolver=None):
        self.db = db
        self.artifacts = artifacts
        self.signing_key = signing_key.encode() if isinstance(signing_key, str) else signing_key
        self.scope_resolver = scope_resolver

    @staticmethod
    def _check_kind(kind):
        if kind not in KINDS:
            raise ValueError("Unknown capability kind")

    async def _validate_scope(self, session, scope):
        row = await session.get(McpServer, scope.server_id, populate_existing=True)
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
            if kind in {"resources", "templates"}:
                summary["uri" if kind == "resources" else "uriTemplate"] = name
            gateway_name = alias(slug, name) if kind == "tools" else None
            if gateway_name:
                summary["gateway_name"] = gateway_name
            schema_hash = await asyncio.to_thread(
                lambda definition=entry: digest(strict_json_dumps(definition.get("inputSchema", {})))
            ) if kind == "tools" else ""
            prepared.append((ref, {
                "generation_id": identity, "kind": kind, "original_name": name, "name_hash": digest(name),
                # A bounded ASCII sort prefix + full hash gives portable deterministic keyset pagination.
                "sort_key": name.encode("utf-8")[:128].hex(),
                "gateway_name": gateway_name, "gateway_hash": digest(gateway_name) if gateway_name else None,
                "legacy_gateway_hash": digest(server_id + "__" + name) if gateway_name else None,
                "schema_hash": schema_hash, "summary": summary,
                "search_text": " ".join(str(entry.get(key, "")) for key in ("name", "title", "description")).casefold(),
                "artifact_id": ref.id, "artifact_bytes": ref.size_bytes,
            }))
        try:
            async with self.db.locked() as session:
                await self._staging(session, identity)
                for ref, values in prepared:
                    if await session.get(ArtifactObject, ref.id) is None:
                        session.add(ArtifactObject(id=ref.id, size_bytes=ref.size_bytes, sha256=ref.sha256))
                        await session.flush()
                    session.add(CatalogEntry(**values))
                await session.flush()
        except IntegrityError:
            raise GatewayError("catalog_duplicate_entry", "Catalog contains duplicate entries or conflicting hashes") from None

    async def publish(self, identity, *, expected_scope):
        # Freeze staging before the bounded streaming digest pass, outside the final publish transaction.
        async with self.db.locked() as session:
            generation, _, _ = await self._staging(session, identity)
            if generation_scope(generation) != expected_scope:
                raise GatewayError("credential_changed", "Catalog refresh does not match the current scope")
            generation.status = "publishing"
        counts = dict.fromkeys(sorted(KINDS), 0)
        accumulator = hashlib.sha256()
        async with self.db.session() as session:
            stream = await session.stream(select(
                CatalogEntry.kind, CatalogEntry.name_hash, CatalogEntry.artifact_id
            ).where(CatalogEntry.generation_id == identity).order_by(
                CatalogEntry.kind, CatalogEntry.name_hash
            ).execution_options(yield_per=250))
            async for kind, name_hash, artifact_id in stream:
                counts[kind] += 1
                accumulator.update(strict_json_dumps([kind, name_hash, artifact_id]).encode())
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
            generation.status, generation.published_at = "ready", now()
            head.generation_id, head.status = identity, "ready"
            await session.flush()
            result = generation_ref(generation)
        return result

    async def _current(self, session, scope):
        row = await session.get(McpServer, scope.server_id, populate_existing=True)
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
            }

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
            binding = [scope_key(scope), generation.id, scope.revision, scope.credential_generation, kind, query, limit]
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
            selected = rows[:limit]
            next_cursor = self._cursor({"binding": binding, "key": [selected[-1].sort_key, selected[-1].name_hash]}) if len(rows) > limit else None
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

    async def resolve_gateway_name(self, gateway_name, allowed_ids, *, owner="service"):
        if not allowed_ids:
            return None
        name_hash = digest(gateway_name)
        async with self.db.session() as session:
            statement = select(CatalogEntry, CatalogGeneration, McpServer).join(
                CatalogGeneration, CatalogGeneration.id == CatalogEntry.generation_id
            ).join(CatalogHead, CatalogHead.generation_id == CatalogGeneration.id).join(
                McpServer, McpServer.id == CatalogGeneration.server_id
            ).where(
                McpServer.id.in_(allowed_ids), McpServer.mode != "disabled",
                McpServer.revision == CatalogGeneration.revision,
                CatalogGeneration.status == "ready", CatalogHead.status == "ready",
                CatalogEntry.kind == "tools",
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

    async def fail_refresh(self, identity):
        async with self.db.locked() as session:
            generation = await session.get(CatalogGeneration, identity)
            if not generation:
                return
            generation.status = "failed"
            head = await session.get(CatalogHead, generation.scope_key)
            if head and head.refresh_id == identity:
                head.status = "error"
