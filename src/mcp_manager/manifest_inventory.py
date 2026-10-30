"""Rebuild historical pins from verified files without restoring a visible catalog."""
import asyncio
import codecs
import json
import re
from contextlib import aclosing
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from .catalog_models import ArtifactObject, CatalogManifest, CatalogObjectRef
from .catalog_types import ArtifactRef
from .database import now
from .runtime import GatewayError

MARKER = ',"entries":['


async def manifest_batches(artifacts, ref):
    """Verify the entire hash first, then parse one bounded batch at a time."""
    if ref.size_bytes > 64 * 1024 * 1024:
        raise GatewayError("artifact_too_large", "Manifest exceeds inventory budget")
    decoder = codecs.getincrementaldecoder("utf-8")()
    parser = json.JSONDecoder()
    buffer, metadata, pending, separator, finished = "", None, [], False, False
    async with aclosing(artifacts.open_download(ref)) as chunks:
        async for raw in chunks:
            buffer += decoder.decode(raw)
            if metadata is None:
                position = buffer.find(MARKER)
                if position < 0:
                    if len(buffer) > 16384:
                        raise GatewayError("artifact_corrupt", "Manifest header exceeds budget")
                    continue
                metadata = json.loads(buffer[:position] + "}")
                buffer = buffer[position + len(MARKER):]
                yield metadata, []
            while not finished:
                buffer = buffer.lstrip()
                if buffer == "]":
                    break
                if buffer.startswith("]}"):
                    buffer, finished = buffer[2:], True
                    break
                if separator:
                    if not buffer:
                        break
                    if buffer[0] != ",":
                        raise GatewayError("artifact_corrupt", "Invalid manifest record separator")
                    buffer, separator = buffer[1:].lstrip(), False
                try:
                    item, end = parser.raw_decode(buffer)
                except json.JSONDecodeError:
                    if len(buffer) > 16 * 1024 * 1024:
                        raise GatewayError("artifact_corrupt", "Manifest entry exceeds budget")
                    break
                if not isinstance(item, dict):
                    raise GatewayError("artifact_corrupt", "Invalid manifest entry")
                pending.append(item)
                buffer, separator = buffer[end:], True
                if len(pending) == 250:
                    yield metadata, pending
                    pending = []
    buffer += decoder.decode(b"", final=True)
    if not finished or buffer.strip() or metadata is None:
        raise GatewayError("artifact_corrupt", "Incomplete manifest inventory")
    if pending:
        yield metadata, pending


def _pointer(path, root):
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise GatewayError("artifact_corrupt", "Manifest pointer leaves managed directory")
    with path.open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise GatewayError("artifact_corrupt", "Manifest pointer exceeds budget")
    value = json.loads(raw)
    if (not re.fullmatch("[0-9a-f]{64}", value.get("hash", ""))
            or not re.fullmatch("[0-9a-f]{64}", value.get("scope_key", ""))
            or not isinstance(value.get("generation"), str) or len(value["generation"]) > 36
            or type(value.get("bytes")) is not int or not 0 <= value["bytes"] <= 64 * 1024 * 1024):
        raise GatewayError("artifact_corrupt", "Invalid manifest pointer")
    return value


async def recover_one(state, path):
    artifacts = state.catalog.artifacts
    root = artifacts.data_dir / "catalog" / "manifests"
    pointer = await asyncio.to_thread(_pointer, path, root)
    ref = ArtifactRef(pointer["hash"], pointer["bytes"], pointer["hash"])
    created = datetime.fromisoformat(pointer["created_at"])
    created = created.replace(tzinfo=UTC) if created.tzinfo is None else created.astimezone(UTC)
    retire = min(created, now()) + timedelta(hours=24)
    count = 0
    async for metadata, entries in manifest_batches(artifacts, ref):
        if (metadata.get("generation"), metadata.get("scope_key")) != (pointer["generation"], pointer["scope_key"]):
            raise GatewayError("artifact_corrupt", "Manifest metadata disagrees with pointer")
        count += len(entries)
        if count > 100000:
            raise GatewayError("artifact_corrupt", "Manifest entry count exceeds budget")
        values = {}
        for item in entries:
            identity, size = item.get("hash"), item.get("bytes")
            if not isinstance(identity, str) or not re.fullmatch("[0-9a-f]{64}", identity) or type(size) is not int or size < 0:
                raise GatewayError("artifact_corrupt", "Invalid manifest object reference")
            if identity in values and values[identity] != size:
                raise GatewayError("artifact_corrupt", "Conflicting manifest object size")
            values[identity] = size
        async with state.db.derived.locked() as session:
            objects = [{"id": identity, "size_bytes": size, "sha256": identity} for identity, size in values.items()]
            if await session.get(ArtifactObject, ref.id) is None:
                session.add(ArtifactObject(id=ref.id, size_bytes=ref.size_bytes, sha256=ref.id))
                await session.flush()
            if await session.get(CatalogManifest, pointer["generation"]) is None:
                session.add(CatalogManifest(id=pointer["generation"], scope_key=pointer["scope_key"],
                    artifact_id=ref.id, artifact_bytes=ref.size_bytes, current=False,
                    created_at=created, retire_after=retire))
                await session.flush()
            if objects:
                previous = dict((await session.execute(select(ArtifactObject.id, ArtifactObject.size_bytes).where(
                    ArtifactObject.id.in_(values)))).all())
                if any(previous[identity] != size for identity, size in values.items() if identity in previous):
                    raise GatewayError("artifact_corrupt", "Manifest object size changed")
                await session.execute(insert(ArtifactObject).on_conflict_do_nothing(), objects)
                await session.execute(insert(CatalogObjectRef).on_conflict_do_nothing(),
                    [{"manifest_id": pointer["generation"], "artifact_id": identity} for identity in values])


async def recover_cache_inventory(state, *, limit=250):
    artifacts = state.catalog.artifacts
    inventory = artifacts.inventory
    if not await asyncio.to_thread(inventory.ready, "cache"):
        await asyncio.to_thread(inventory.scan_next, "cache", limit=limit)
    if not await asyncio.to_thread(inventory.ready, "durable"):
        await asyncio.to_thread(inventory.scan_next, "durable", limit=limit)
    if not await asyncio.to_thread(inventory.roots_ready, "cache"):
        cursor = getattr(state, "_manifest_inventory_cursor", None)
        if cursor is None:
            root = artifacts.data_dir / "catalog" / "manifests"
            state._manifest_inventory_cursor = cursor = root.glob("*/*/*.json")
        paths = []
        for _ in range(min(limit, 10)):
            path = next(cursor, None)
            if path is None:
                break
            paths.append(path)
        for path in paths:
            await recover_one(state, path)
        if len(paths) < min(limit, 10):
            await asyncio.to_thread(inventory.finish_roots, "cache")
            state._manifest_inventory_cursor = None
    return (await asyncio.to_thread(inventory.ready, "cache")
            and await asyncio.to_thread(inventory.roots_ready, "cache"))
