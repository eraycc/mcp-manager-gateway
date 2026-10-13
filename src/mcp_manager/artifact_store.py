"""Bounded content-addressed blobs. Callers must authorize references before reading."""
import asyncio
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path
from uuid import uuid4

from .catalog_types import ArtifactRef
from .runtime import GatewayError

CHUNK_BYTES = 65536


class ArtifactStore:
    def __init__(self, data_dir, *, cache_bytes=128 * 1024 * 1024, gc_grace_seconds=3600):
        self.root = (Path(data_dir) / "objects").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_bytes = max(0, cache_bytes)
        self.gc_grace_seconds = max(0, gc_grace_seconds)
        self._cache = OrderedDict()
        self._cache_size = 0
        self._pins = Counter()
        self._lock = threading.RLock()
        self._gc_cursor = None

    @staticmethod
    def _validate(ref):
        if (
            not isinstance(ref, ArtifactRef) or not re.fullmatch(r"[0-9a-f]{64}", ref.id)
            or ref.sha256 != ref.id or not isinstance(ref.size_bytes, int) or ref.size_bytes < 0
        ):
            raise ValueError("Invalid artifact reference")

    def _path(self, identity):
        path = self.root / identity[:2] / (identity + ".json")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Artifact path leaves the object root")
        return path

    def _remember(self, ref, raw):
        with self._lock:
            if len(raw) > self.cache_bytes:
                return
            previous = self._cache.pop(ref.id, None)
            if previous is not None:
                self._cache_size -= len(previous)
            self._cache[ref.id] = raw
            self._cache_size += len(raw)
            while self._cache_size > self.cache_bytes:
                _, evicted = self._cache.popitem(last=False)
                self._cache_size -= len(evicted)

    @staticmethod
    def _too_large():
        return GatewayError("artifact_too_large", "Artifact exceeds the byte budget; use a bounded download")

    async def put_json(self, value, *, max_bytes):
        if not isinstance(value, dict) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("JSON artifacts require an object and a positive byte budget")
        return await asyncio.to_thread(self._put_json, value, max_bytes)

    def _put_json(self, value, max_bytes):
        # Staging is under the fixed object root, never a user-supplied path.
        staging = self.root / ("." + uuid4().hex + ".tmp")
        digest, size = hashlib.sha256(), 0
        try:
            with staging.open("xb") as stream:
                encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                for piece in encoder.iterencode(value):
                    raw = piece.encode("utf-8")
                    size += len(raw)
                    if size > max_bytes:
                        raise self._too_large()
                    digest.update(raw)
                    stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            identity = digest.hexdigest()
            target = self._path(identity)
            target.parent.mkdir(exist_ok=True)
            # Move the durable spool into a same-directory staging file before publishing.
            bucket_staging = target.parent / ("." + uuid4().hex + ".tmp")
            os.replace(staging, bucket_staging)
            staging = bucket_staging
            os.replace(staging, target)
            if os.name != "nt":
                directory = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                root_directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(root_directory)
                finally:
                    os.close(root_directory)
            return ArtifactRef(identity, size, identity)
        finally:
            staging.unlink(missing_ok=True)

    def _read_raw(self, ref, max_bytes):
        self._validate(ref)
        if ref.size_bytes > max_bytes:
            raise self._too_large()
        with self._lock:
            raw = self._cache.get(ref.id)
            if raw is not None:
                self._cache.move_to_end(ref.id)
        if raw is None:
            with self._path(ref.id).open("rb") as stream:
                raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise self._too_large()
        if len(raw) != ref.size_bytes or hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise GatewayError("artifact_corrupt", "Artifact integrity verification failed")
        self._remember(ref, raw)
        return raw

    async def read_json(self, ref, *, max_bytes):
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("A positive read byte budget is required")
        def read():
            value = json.loads(self._read_raw(ref, max_bytes))
            if not isinstance(value, dict):
                raise GatewayError("artifact_corrupt", "Artifact is not a JSON object")
            return value
        return await asyncio.to_thread(read)

    def _open_verified(self, ref):
        self._validate(ref)
        with self._lock:
            self._pins[ref.id] += 1
            try:
                stream = self._path(ref.id).open("rb")
            except BaseException:
                self._pins[ref.id] -= 1
                raise
        try:
            digest, size = hashlib.sha256(), 0
            while raw := stream.read(CHUNK_BYTES):
                size += len(raw)
                if size > ref.size_bytes:
                    raise GatewayError("artifact_corrupt", "Artifact size changed")
                digest.update(raw)
            if size != ref.size_bytes or digest.hexdigest() != ref.sha256:
                raise GatewayError("artifact_corrupt", "Artifact integrity verification failed")
            stream.seek(0)
            return stream
        except BaseException:
            self._close_stream(ref, stream)
            raise

    def _close_stream(self, ref, stream):
        try:
            stream.close()
        finally:
            with self._lock:
                self._pins[ref.id] -= 1
                if not self._pins[ref.id]:
                    del self._pins[ref.id]

    async def open_download(self, ref):
        # No hash-only public route: authorization resolves this trusted reference first.
        opening = asyncio.create_task(asyncio.to_thread(self._open_verified, ref))
        try:
            stream = await asyncio.shield(opening)
        except asyncio.CancelledError:
            async def cleanup():
                try:
                    abandoned = await opening
                except (OSError, GatewayError, ValueError):
                    return
                await asyncio.to_thread(self._close_stream, ref, abandoned)
            cleanup_task = asyncio.create_task(cleanup())
            await asyncio.shield(cleanup_task)
            raise
        try:
            while raw := await asyncio.to_thread(stream.read, CHUNK_BYTES):
                yield raw
        finally:
            await asyncio.to_thread(self._close_stream, ref, stream)

    def _object_paths(self):
        with os.scandir(self.root) as buckets:
            for bucket in buckets:
                if bucket.is_dir(follow_symlinks=False) and re.fullmatch(r"[0-9a-f]{2}", bucket.name):
                    with os.scandir(bucket.path) as entries:
                        for entry in entries:
                            if entry.is_file(follow_symlinks=False) and re.fullmatch(r"[0-9a-f]{64}\.json", entry.name):
                                yield Path(entry.path)

    async def gc(self, live_ids, *, limit=250):
        if not isinstance(limit, int) or not 1 <= limit <= 250:
            raise ValueError("GC batch must contain 1–250 items")
        live_ids = frozenset(live_ids)
        return await asyncio.to_thread(self._gc, live_ids, limit)

    def _gc(self, live_ids, limit):
        removed = 0
        with self._lock:
            if self._gc_cursor is None:
                self._gc_cursor = self._object_paths()
            for _ in range(limit):
                path = next(self._gc_cursor, None)
                if path is None:
                    self._gc_cursor = None
                    break
                identity = path.stem
                if identity in live_ids or self._pins[identity]:
                    continue
                try:
                    if time.time() - path.stat().st_mtime < self.gc_grace_seconds:
                        continue
                    path.unlink()
                except FileNotFoundError:
                    continue
                cached = self._cache.pop(identity, None)
                if cached is not None:
                    self._cache_size -= len(cached)
                removed += 1
        return removed
