"""Primary-backed governance policy and bounded content-free storage accounting."""
import asyncio
import time

from .database import SystemSetting, get_setting
from .storage_admission import StorageAdmission
from .storage_async import run_blocking
from .storage_inventory import StorageInventory
from .storage_policy import DEFAULT_STORAGE_POLICY, STORAGE_POLICY_KEY, normalize_storage_policy


async def initialize_policy(db):
    """Restore the disposable mirror before any application CAS writer starts."""
    from .object_inventory import ObjectInventory

    async with db.session() as session:
        row = await session.get(SystemSetting, STORAGE_POLICY_KEY)
        policy = normalize_storage_policy(row.value if row else {})
    def mirror():
        inventory = ObjectInventory(db.settings.data_dir)
        inventory.close()
        admission = StorageAdmission(db.settings.data_dir)
        try:
            if admission.policy() != policy:
                admission.set_policy(policy)
        finally:
            admission.close()
    await run_blocking(mirror)
    return policy


class StorageGovernance:
    def __init__(self, state):
        self.state = state
        self.admission = state.catalog.artifacts.admission
        self.policy = dict(DEFAULT_STORAGE_POLICY)
        self.inventory = StorageInventory(state.config.data_dir, primary_path=state.storage.path("main"))
        self.files = self.inventory._report(0)
        self.scan_guard = asyncio.Lock()
        self.scanned_at = 0

    def _artifacts(self):
        stores = [self.state.catalog.artifacts, self.state.jobs.store.artifacts,
                  self.state.catalog.cache_store.artifacts, self.state.embeddings.store.artifacts]
        return list({id(store): store for store in stores}.values())

    def _trim_artifacts(self):
        # Compatibility values already have their own small byte-LRU. Their
        # ArtifactStore is a disk reader and must keep its extra body cache off.
        stores = list({id(store): store for store in (
            self.state.catalog.artifacts, self.state.jobs.store.artifacts)}.values())
        compatibility = (self.state.catalog.cache_store, self.state.embeddings.store)
        part = min(2 * 1024**2, self.policy["artifact_memory_bytes"] // 8)
        for cache in compatibility:
            with cache.cache_lock:
                cache.max_cache_bytes = part
                while cache.cache_size > part:
                    _, raw = cache.cache.popitem(last=False)
                    cache.cache_size -= len(raw)
        budget = (self.policy["artifact_memory_bytes"] - part * len(compatibility)) // len(stores)
        for store in stores:
            with store._lock:
                store.cache_bytes = budget
                while store._cache_size > budget:
                    _, raw = store._cache.popitem(last=False)
                    store._cache_size -= len(raw)

    async def _apply_memory(self):
        await run_blocking(self._trim_artifacts)
        snapshots = self.state.gateway.discovery.snapshots
        index = self.state.catalog.index
        index._search_cache_max_bytes = min(8 * 1024**2, self.policy["snapshot_memory_bytes"] // 4)
        async with index._search_cache_lock:
            while index._search_cache_bytes > index._search_cache_max_bytes:
                key, _ = index._search_cache.popitem(last=False)
                index._search_cache_bytes -= index._search_sizes.pop(key)
        snapshots.max_bytes = self.policy["snapshot_memory_bytes"] - index._search_cache_max_bytes
        if snapshots.bytes > snapshots.max_bytes:
            snapshots.clear()
        for cache in (self.state.catalog.cache_store, self.state.embeddings.store):
            cache.max_rows = self.policy["query_cache_rows"]
            cache.max_body_bytes = self.policy["query_cache_bytes"]
        self.state.embeddings.store.ttl_seconds = self.policy["query_cache_ttl_seconds"]

    async def refresh(self):
        # Serialize mirrors with primary policy writers, preventing a stale worker
        # from restoring an older snapshot after a newer policy has committed.
        async with self.state.db.locked() as session:
            row = await session.get(SystemSetting, STORAGE_POLICY_KEY)
            policy = normalize_storage_policy(row.value if row else {})
            def mirror():
                if self.admission.policy() != policy:
                    self.admission.set_policy(policy)
            await run_blocking(mirror)
        self.policy = policy
        await self._apply_memory()
        return policy

    async def scan(self):
        if self.scan_guard.locked():
            return self.files
        async with self.scan_guard:
            if self.inventory.complete and time.monotonic() - self.scanned_at >= 30:
                await run_blocking(self.inventory.reset)
            self.files = await run_blocking(self.inventory.scan_next)
            if self.files["complete"]:
                self.scanned_at = time.monotonic()
            return self.files

    async def describe(self):
        await self.scan()
        usage = await run_blocking(self.admission.usage)
        def object_usage():
            with self.admission.connect() as connection:
                return {namespace: {"bytes": size, "objects": count,
                                    "inventory_ready": bool(ready), "roots_ready": bool(roots)}
                        for namespace, size, count, ready, roots in connection.execute(
                            "SELECT namespace,body_bytes,objects_count,inventory_ready,roots_ready FROM object_domain_usage")}
        objects = await run_blocking(object_usage)
        def sql_usage():
            with self.admission.connect() as connection:
                rows = {name: {"estimated_bytes": size, "rows": count}
                        for name, size, count in connection.execute(
                            "SELECT name,estimated_bytes,rows_count FROM catalog_sql_usage")}
                retired = connection.execute("""SELECT COALESCE(sum(u.estimated_bytes),0)
                    FROM catalog_generation_usage u WHERE NOT EXISTS(
                        SELECT 1 FROM catalog_heads h WHERE h.generation_id=u.id
                        OR h.pending_id=u.id OR h.refresh_id=u.id)""").fetchone()[0]
            from pathlib import Path
            try:
                wal_bytes = Path(str(self.admission.path) + "-wal").stat().st_size
            except FileNotFoundError:
                wal_bytes = 0
            return {"tables": rows, "estimated_bytes": sum(row["estimated_bytes"] for row in rows.values()),
                    "retiring_estimated_bytes": retired, "wal_allocated_bytes": wal_bytes,
                    "wal_warning": wal_bytes >= self.policy["wal_warn_bytes"]}
        sql = await run_blocking(sql_usage)
        calls = await get_setting(self.state.db, "log_retention_days", 0)
        audit = await get_setting(self.state.db, "audit_log_retention_days", 0)
        return {"policy": dict(self.policy), "files": self.files, "objects": objects, "staging": usage, "sql": sql,
                "retention": {"calls": {"days": calls, "permanent": calls == 0},
                              "audit": {"days": audit, "permanent": audit == 0}},
                "gc": self.state.storage_governor.last_result,
                "memory": {"directory_bytes": self.state.gateway.discovery.snapshots.bytes + self.state.catalog.index._search_cache_bytes,
                           "artifact_bytes": sum(store._cache_size for store in self._artifacts())
                           + self.state.catalog.cache_store.cache_size + self.state.embeddings.store.cache_size}}

    async def close(self):
        async with self.scan_guard:
            await run_blocking(self.inventory.reset)
