"""Stream immutable manifest bodies while keeping historical SQL roots compact."""
import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from .catalog_models import ArtifactObject, CatalogEntry, CatalogManifest, CatalogObjectRef
from .catalog_types import ArtifactRef
from .database import now
from .db_types import strict_json_dumps
from .storage_async import run_blocking


def manifest_pointer(data_dir, scope_key, identity):
    # Keep managed paths short enough for ordinary Windows rename APIs.
    key = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return Path(data_dir) / "catalog" / "manifests" / key[:2] / key[2:4] / (key + ".json")


def _publish_pointer(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if (previous.get("generation"), previous.get("scope_key")) != (value["generation"], value["scope_key"]):
            raise RuntimeError("Manifest pointer hash collision")
    temporary = path.parent / ("." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(strict_json_dumps(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            for folder in (path.parent, path.parent.parent, path.parent.parent.parent):
                descriptor = os.open(folder, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


async def build_manifest(store, generation, counts, digest):
    async with store.artifacts.admission.async_reserve("cache", 65536) as reservation:
        return await _build_reserved_manifest(store, generation, counts, digest, reservation)


async def _build_reserved_manifest(store, generation, counts, digest, reservation):
    artifacts = store.artifacts
    staging = artifacts.root / ("." + reservation + ".tmp")
    accumulator, size, first = hashlib.sha256(), 0, True
    reserved = 65536
    stamp = now()
    metadata = {"format": 1, "generation": generation.id, "scope_key": generation.scope_key,
                "server_id": generation.server_id, "owner": generation.owner,
                "revision": generation.revision, "credential_generation": generation.credential_generation,
                "digest": digest, "counts": counts, "created_at": stamp.isoformat()}

    def write(stream, raw):
        nonlocal size, reserved
        size += len(raw)
        if size > 64 * 1024 * 1024:
            raise ValueError("Catalog manifest exceeds 64 MiB budget")
        if size > reserved:
            reserved = min(64 * 1024 * 1024, max(size, reserved * 2))
            artifacts.admission.grow(reservation, reserved)
        accumulator.update(raw)
        stream.write(raw)

    async with store.db.locked() as session:
        if await session.get(CatalogManifest, generation.id) is None:
            session.add(CatalogManifest(id=generation.id, scope_key=generation.scope_key,
                                       created_at=stamp, retire_after=stamp + timedelta(hours=1)))
    try:
        with staging.open("xb") as stream:
            await run_blocking(write, stream, (strict_json_dumps(metadata)[:-1] + ',"entries":[').encode())
            previous = ""
            while True:
                async with store.db.session() as session:
                    rows = (await session.execute(select(
                        CatalogEntry.id, CatalogEntry.kind, CatalogEntry.original_name,
                        CatalogEntry.artifact_id, CatalogEntry.artifact_bytes
                    ).where(CatalogEntry.generation_id == generation.id, CatalogEntry.id > previous
                    ).order_by(CatalogEntry.id).limit(250))).all()
                if not rows:
                    break
                previous = rows[-1].id
                records = [{"kind": row.kind, "name": row.original_name,
                            "hash": row.artifact_id, "bytes": row.artifact_bytes} for row in rows]
                raw = (("" if first else ",") + ",".join(strict_json_dumps(record) for record in records)).encode()
                first = False
                await run_blocking(write, stream, raw)
                async with store.db.locked() as session:
                    from .catalog_sql_budget import check_sql_budget
                    await check_sql_budget(session, len(rows) * 160)
                    await session.execute(insert(CatalogObjectRef).on_conflict_do_nothing(),
                        [{"manifest_id": generation.id, "artifact_id": row.artifact_id} for row in rows])
            await run_blocking(write, stream, b"]}")
            await run_blocking(stream.flush)
            await run_blocking(os.fsync, stream.fileno())
        identity = accumulator.hexdigest()
        ref = ArtifactRef(identity, size, identity)
        # Publish on the same filesystem, protected against collection.
        async with artifacts.hold([ref], verify=False), artifacts.gate.async_operation():
            await run_blocking(artifacts.inventory.reserve, ref, artifacts.quota_bytes)
            target = artifacts._path(identity)
            await run_blocking(target.parent.mkdir, parents=True, exist_ok=True)
            await run_blocking(os.replace, staging, target)
            await run_blocking(artifacts.inventory.confirm, ref)
            if os.name != "nt":
                for folder in (target.parent, target.parent.parent, artifacts.root):
                    descriptor = os.open(folder, os.O_RDONLY)
                    try:
                        await run_blocking(os.fsync, descriptor)
                    finally:
                        os.close(descriptor)
            async with store.db.locked() as session:
                if await session.get(ArtifactObject, ref.id) is None:
                    session.add(ArtifactObject(id=ref.id, size_bytes=ref.size_bytes, sha256=ref.id))
                    await session.flush()
                manifest = await session.get(CatalogManifest, generation.id)
                manifest.artifact_id, manifest.artifact_bytes = ref.id, ref.size_bytes
            await run_blocking(_publish_pointer,
                manifest_pointer(artifacts.data_dir, generation.scope_key, generation.id),
                {**metadata, "hash": ref.id, "bytes": ref.size_bytes})
        return ref
    finally:
        await run_blocking(staging.unlink, missing_ok=True)
