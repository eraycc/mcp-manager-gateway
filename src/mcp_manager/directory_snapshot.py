"""Bounded metadata-only snapshots, always fenced by a fresh authoritative clock."""
import asyncio
import sys
from collections import OrderedDict
from contextlib import asynccontextmanager
from types import SimpleNamespace
from weakref import WeakValueDictionary

from .runtime import GatewayError


def retained_bytes(value):
    seen, pending, total = set(), [value], 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
        elif isinstance(item, SimpleNamespace) or item.__class__.__name__ == "CatalogScope":
            pending.append(vars(item))
    return total


class DirectorySnapshots:
    def __init__(self, *, max_bytes=64 * 1024 * 1024, max_entries=32):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.bytes = 0
        self.version = None
        self.entries = OrderedDict()
        self.build_locks = WeakValueDictionary()
        self.capacity = asyncio.Semaphore(2)
        self.pending = 0

    def clear(self):
        self.entries.clear()
        self.bytes = 0

    def _version(self, version):
        if version != self.version:
            self.clear()
            self.version = version

    def get(self, allowed_hash, owner, version):
        self._version(version)
        for key in ((allowed_hash, owner), (allowed_hash, None)):
            if key in self.entries:
                value = self.entries.pop(key)
                self.entries[key] = value
                return value[0]
        return None

    @asynccontextmanager
    async def build(self, allowed_hash, version):
        if self.pending >= 64:
            raise GatewayError("storage_busy", "Directory build queue is full; retry read-only discovery later")
        self.pending += 1
        lock = self.build_locks.setdefault((allowed_hash, version), asyncio.Lock())
        held = False
        try:
            try:
                await asyncio.wait_for(lock.acquire(), 10)
                held = True
                await asyncio.wait_for(self.capacity.acquire(), 10)
            except TimeoutError:
                raise GatewayError("storage_busy", "Directory build is busy; retry read-only discovery later") from None
            try:
                yield
            finally:
                self.capacity.release()
        finally:
            if held:
                lock.release()
            self.pending -= 1

    def prepare(self, rows, scopes, indexed):
        fields = ("id", "name", "slug", "description", "tags", "transport", "mode",
                  "isolation", "revision", "auth_type", "config_isolation", "created_at")
        safe_rows = tuple(SimpleNamespace(**{
            field: tuple(getattr(row, field)) if field == "tags" else getattr(row, field)
            for field in fields if hasattr(row, field)
        }) for row in rows)
        # These newly built dictionaries stay internal and are read-only after
        # admission. Response lists are copied at their projection boundary.
        payload = safe_rows, dict(scopes), indexed
        return payload, retained_bytes(payload)

    def put(self, allowed_hash, owner, version, prepared):
        self._version(version)
        payload, size = prepared
        if size > self.max_bytes:
            return
        if all(row.isolation != "user" for row in payload[0]):
            owner = None
        key = allowed_hash, owner
        previous = self.entries.pop(key, None)
        if previous:
            self.bytes -= previous[1]
        self.entries[key] = (payload, size)
        self.bytes += size
        while self.bytes > self.max_bytes or len(self.entries) > self.max_entries:
            _, (_, previous_size) = self.entries.popitem(last=False)
            self.bytes -= previous_size

    def get_scores(self, key, version):
        self._version(version)
        identity = ("scores", key)
        value = self.entries.pop(identity, None)
        if value is not None:
            self.entries[identity] = value
            return value[0]
        return None

    def put_scores(self, key, version, prepared):
        self._version(version)
        value, size = prepared
        if size > self.max_bytes:
            return
        identity = ("scores", key)
        previous = self.entries.pop(identity, None)
        if previous:
            self.bytes -= previous[1]
        self.entries[identity] = (value, size)
        self.bytes += size
        while self.bytes > self.max_bytes or len(self.entries) > self.max_entries:
            _, (_, size) = self.entries.popitem(last=False)
            self.bytes -= size
