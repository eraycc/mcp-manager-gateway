"""Bounded content-addressed blobs. Callers must authorize references before reading."""
import asyncio
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from filelock import FileLock

from .catalog_types import ArtifactRef
from .runtime import GatewayError
from .storage_async import join_task, run_blocking

CHUNK_BYTES = 65536


class ArtifactStore:
    def __init__(self, data_dir, *, cache_bytes=128 * 1024 * 1024, gc_grace_seconds=3600, namespace="cache", _gate=None, _admission=None, _inventory=None):
        if namespace not in {"cache", "durable"}:
            raise ValueError("Invalid artifact namespace")
        self.data_dir = Path(data_dir).resolve()
        self.namespace = namespace
        from .object_inventory import ObjectInventory
        from .storage_gate import StorageGate
        self.gate = _gate or StorageGate(self.data_dir)
        self._owns_gate = _gate is None
        self._owns_inventory = _inventory is None
        try:
            with self.gate.operation():
                self.inventory = _inventory or ObjectInventory(self.data_dir)
            from .storage_admission import StorageAdmission
            self.admission = _admission or StorageAdmission(self.data_dir, gate=self.gate)
            self._owns_admission = _admission is None
            self.quota_bytes = None
            self.root = self.data_dir / "objects" / namespace / "sha256"
            self.root.mkdir(parents=True, exist_ok=True)
            self.cache_bytes = max(0, cache_bytes)
            self.gc_grace_seconds = max(0, gc_grace_seconds)
            self._cache = OrderedDict()
            self._cache_size = 0
            self._pins = Counter()
            self._lock = threading.RLock()
            self._gc_cursor = None
            self.lock_root = self.data_dir / "locks" / "objects"
            self.lock_root.mkdir(parents=True, exist_ok=True)

        except BaseException:
            if getattr(self, "_owns_admission", False):
                self.admission.close()
            if self._owns_inventory and hasattr(self, "inventory"):
                self.inventory.close()
            if self._owns_gate:
                self.gate.close()
            raise

    def _stripe(self, identity, namespace=None):
        namespace = self.namespace if namespace is None else namespace
        # 256 fixed lock files per domain; never one permanent file per object.
        return FileLock(str(self.lock_root / (namespace + "-" + identity[:2] + ".lock")),
                        timeout=30, thread_local=False, preserve_lock_file=True)

    @asynccontextmanager
    async def hold(self, refs, *, verify=True, timeout=30):
        """Keep attach/root commit, reading and collection mutually exclusive."""
        refs = list(refs)
        for ref in refs:
            self._validate(ref)
        locks = {}
        for ref in refs:
            key = (ref.namespace, ref.id[:2])
            if key not in locks:
                locks[key] = self._stripe(ref.id, ref.namespace)
        acquired = []
        try:
            for key in sorted(locks):
                guard = locks[key]
                waiting = asyncio.create_task(asyncio.to_thread(guard.acquire, timeout=timeout))
                try:
                    await asyncio.shield(waiting)
                except asyncio.CancelledError:
                    await join_task(waiting)
                    await run_blocking(guard.release)
                    raise
                acquired.append(guard)
            if verify:
                def verify_bodies():
                    for ref in refs:
                        path = self._path(ref.id, ref.namespace)
                        try:
                            size = path.stat().st_size
                        except FileNotFoundError:
                            raise GatewayError("artifact_missing", "Artifact body is missing; root was not committed") from None
                        if size != ref.size_bytes:
                            raise GatewayError("artifact_corrupt", "Artifact size changed before root commit")
                await run_blocking(verify_bodies)
            yield
        finally:
            for guard in reversed(acquired):
                await run_blocking(guard.release)

    def for_namespace(self, namespace):
        if namespace == self.namespace:
            return self
        return ArtifactStore(self.data_dir, namespace=namespace, cache_bytes=self.cache_bytes,
                             gc_grace_seconds=self.gc_grace_seconds, _gate=self.gate, _admission=self.admission, _inventory=self.inventory)

    @staticmethod
    def _validate(ref):
        if (
            not isinstance(ref, ArtifactRef) or not re.fullmatch(r"[0-9a-f]{64}", ref.id)
            or ref.sha256 != ref.id or not isinstance(ref.size_bytes, int) or ref.size_bytes < 0
            or ref.namespace not in {"cache", "durable"}
        ):
            raise ValueError("Invalid artifact reference")

    def _path(self, identity, namespace=None):
        # Every caller supplies either a locally computed SHA-256 or a validated
        # ArtifactRef. Lexical digest validation avoids a racy Windows resolve()
        # through a bucket directory being concurrently created.
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("Artifact path leaves the object root")
        namespace = self.namespace if namespace is None else namespace
        if namespace not in {"cache", "durable"}:
            raise ValueError("Invalid artifact namespace")
        root = self.data_dir / "objects" / namespace / "sha256"
        return root / identity[:2] / identity[2:4] / (identity + ".json")

    def _remember(self, ref, raw):
        with self._lock:
            if len(raw) > self.cache_bytes:
                return
            previous = self._cache.pop((ref.namespace, ref.id), None)
            if previous is not None:
                self._cache_size -= len(previous)
            self._cache[(ref.namespace, ref.id)] = raw
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
        return await run_blocking(self._put_json, value, max_bytes)

    def _put_json(self, value, max_bytes):
        with self.admission.reserve(self.namespace, min(max_bytes, CHUNK_BYTES)) as reservation:
            return self._put_reserved_json(value, max_bytes, reservation)

    def _put_reserved_json(self, value, max_bytes, reservation):
        # Staging is under the fixed object root, never a user-supplied path.
        staging = self.root / ("." + reservation + ".tmp")
        digest, size, ref = hashlib.sha256(), 0, None
        published = False
        reserved = min(max_bytes, CHUNK_BYTES)
        try:
            with staging.open("xb") as stream:
                encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                for piece in encoder.iterencode(value):
                    raw = piece.encode("utf-8")
                    size += len(raw)
                    if size > max_bytes:
                        raise self._too_large()
                    if size > reserved:
                        reserved = min(max_bytes, max(size, reserved * 2))
                        self.admission.grow(reservation, reserved)
                    digest.update(raw)
                    stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            identity = digest.hexdigest()
            target = self._path(identity)
            ref = ArtifactRef(identity, size, identity, self.namespace)
            with self._stripe(identity), self.gate.operation(), self._lock:
                self.inventory.reserve(ref, self.quota_bytes)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    # Reusing an old object refreshes its grace window until the
                    # caller commits the new database reference.
                    if target.stat().st_size != size:
                        raise GatewayError("artifact_corrupt", "Existing object size disagrees with its identity")
                    os.utime(target, None)
                    self.inventory.confirm(ref)
                    published = True
                    return ref
                # The spool and final body share a filesystem. A single atomic
                # cross-directory rename needs no second temporary metadata commit.
                os.replace(staging, target)
                self.inventory.confirm(ref)
                published = True
            if os.name != "nt":
                # Persist each new shard directory and its parent link.
                for parent in (target.parent, target.parent.parent, self.root, self.root.parent, self.root.parent.parent):
                    directory = os.open(parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            return ArtifactRef(identity, size, identity, self.namespace)
        finally:
            staging.unlink(missing_ok=True)
            if ref is not None and not published:
                with self._stripe(ref.id), self.gate.operation():
                    if not self._path(ref.id).exists():
                        self.inventory.forget(ref.namespace, ref.id)

    def _read_disk_raw(self, ref, max_bytes):
        self._validate(ref)
        if ref.size_bytes > max_bytes:
            raise self._too_large()
        with self._path(ref.id, ref.namespace).open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise self._too_large()
        if len(raw) != ref.size_bytes or hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise GatewayError("artifact_corrupt", "Artifact integrity verification failed")
        return raw

    def _read_raw(self, ref, max_bytes):
        self._validate(ref)
        if ref.size_bytes > max_bytes:
            raise self._too_large()
        with self._lock:
            raw = self._cache.get((ref.namespace, ref.id))
            if raw is not None:
                self._cache.move_to_end((ref.namespace, ref.id))
        if raw is None:
            with self._path(ref.id, ref.namespace).open("rb") as stream:
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
        self._validate(ref)
        if ref.size_bytes > max_bytes:
            raise self._too_large()
        # A local reference pins these immutable verified bytes even if GC
        # removes their disk file or evicts the LRU concurrently. Cold reads
        # still hold the collector stripe while opening and verifying disk.
        with self._lock:
            cached = self._cache.get((ref.namespace, ref.id))
            if cached is not None:
                self._cache.move_to_end((ref.namespace, ref.id))
        def read():
            raw = cached if cached is not None else self._read_raw(ref, max_bytes)
            if len(raw) != ref.size_bytes or hashlib.sha256(raw).hexdigest() != ref.sha256:
                raise GatewayError("artifact_corrupt", "Artifact integrity verification failed")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise GatewayError("artifact_corrupt", "Artifact is not a JSON object")
            return value
        if cached is not None:
            return await run_blocking(read)
        async with self.hold([ref]):
            return await run_blocking(read)

    def _open_verified(self, ref):
        self._validate(ref)
        guard = self._stripe(ref.id, ref.namespace)
        guard.acquire()
        with self._lock:
            self._pins[(ref.namespace, ref.id)] += 1
            try:
                stream = self._path(ref.id, ref.namespace).open("rb")
                stream._artifact_guard = guard
            except BaseException:
                self._pins[(ref.namespace, ref.id)] -= 1
                guard.release()
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
            stream._artifact_guard.release()
            with self._lock:
                self._pins[(ref.namespace, ref.id)] -= 1
                if not self._pins[(ref.namespace, ref.id)]:
                    del self._pins[(ref.namespace, ref.id)]

    async def open_download(self, ref):
        # No hash-only public route: authorization resolves this trusted reference first.
        opening = asyncio.create_task(asyncio.to_thread(self._open_verified, ref))
        try:
            stream = await asyncio.shield(opening)
        except asyncio.CancelledError:
            try:
                abandoned = await join_task(opening)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001,S110 - consume failed open after cancellation
                pass
            else:
                await run_blocking(self._close_stream, ref, abandoned)
            raise
        try:
            while raw := await run_blocking(stream.read, CHUNK_BYTES):
                yield raw
        finally:
            await run_blocking(self._close_stream, ref, stream)

    def _object_paths(self):
        with os.scandir(self.root) as buckets:
            for bucket in buckets:
                if not bucket.is_dir(follow_symlinks=False) or not re.fullmatch(r"[0-9a-f]{2}", bucket.name):
                    continue
                with os.scandir(bucket.path) as shards:
                    for shard in shards:
                        if not shard.is_dir(follow_symlinks=False) or not re.fullmatch(r"[0-9a-f]{2}", shard.name):
                            continue
                        with os.scandir(shard.path) as entries:
                            for entry in entries:
                                if entry.is_file(follow_symlinks=False) and re.fullmatch(r"[0-9a-f]{64}\.json", entry.name):
                                    yield Path(entry.path)

    async def discard(self, identities, *, limit=250):
        if not isinstance(limit, int) or not 1 <= limit <= 250:
            raise ValueError("GC batch must contain 1–250 items")
        from itertools import islice
        bounded = list(dict.fromkeys(islice(identities, limit)))
        removed = []
        for identity in bounded:
            self._path(identity)  # Validate before constructing a lock path.
            guard = self._stripe(identity)
            waiting = asyncio.create_task(asyncio.to_thread(guard.acquire))
            try:
                await asyncio.shield(waiting)
            except asyncio.CancelledError:
                await join_task(waiting)
                await run_blocking(guard.release)
                raise
            try:
                removed.extend(await run_blocking(self._discard, [identity]))
            finally:
                await run_blocking(guard.release)
        return removed

    def _discard(self, identities):
        removed = []
        with self._lock:
            for identity in identities:
                path = self._path(identity)
                if self._pins[(self.namespace, identity)]:
                    continue
                try:
                    if time.time() - path.stat().st_mtime < self.gc_grace_seconds:
                        continue
                    path.unlink()
                except FileNotFoundError:
                    pass
                cached = self._cache.pop((self.namespace, identity), None)
                if cached is not None:
                    self._cache_size -= len(cached)
                self.inventory.forget(self.namespace, identity)
                removed.append(identity)
        return removed

    async def gc(self, live_ids, *, limit=250):
        if not isinstance(limit, int) or not 1 <= limit <= 250:
            raise ValueError("GC batch must contain 1–250 items")
        live_ids = frozenset(live_ids)
        return await run_blocking(self._gc, live_ids, limit)

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
                if identity in live_ids or self._pins[(self.namespace, identity)]:
                    continue
                try:
                    if time.time() - path.stat().st_mtime < self.gc_grace_seconds:
                        continue
                    path.unlink()
                except FileNotFoundError:
                    continue
                cached = self._cache.pop((self.namespace, identity), None)
                if cached is not None:
                    self._cache_size -= len(cached)
                self.inventory.forget(self.namespace, identity)
                removed += 1
        return removed

    def close(self):
        if self._owns_admission:
            self.admission.close()
        if self._owns_inventory:
            self.inventory.close()
        if self._owns_gate:
            self.gate.close()
