"""Independent, single-leader cleanup with fair batches and a bounded duty cycle."""
import asyncio
import time

from filelock import FileLock, Timeout

from .database import now
from .manifest_inventory import recover_cache_inventory
from .retention import drain_retired_postings, run_storage_retention
from .storage_async import run_blocking


class StorageGovernor:
    def __init__(self, state):
        self.state = state
        self.leader = FileLock(str(state.config.data_dir / "locks/storage-gc.lock"),
                               timeout=0, thread_local=False)
        self.owns_leader = False
        self.guard = asyncio.Lock()
        self.task = None
        self.last_result = None
        self.failures = 0
        self.policy_refreshed = 0

    def _claim(self):
        if not self.owns_leader:
            try:
                self.leader.acquire(timeout=0)
            except Timeout:
                return False
            self.owns_leader = True
        return True

    async def run_once(self, *, seconds=2, row_budget=20000):
        if not 0 < seconds <= 5 or type(row_budget) is not int or not 250 <= row_budget <= 50000:
            raise ValueError("Invalid storage cleanup duty budget")
        if self.guard.locked():
            return {"status": "busy", "deleted": 0}
        async with self.guard:
            if not await run_blocking(self._claim):
                return {"status": "another_worker", "deleted": 0}
            started = time.monotonic()
            governance = getattr(self.state, "storage_governance", None)
            released, orphaned = 0, 0
            if governance is not None:
                released = (await run_blocking(governance.admission.reap_abandoned))["released"]
                await governance.scan()
                for cache in (self.state.catalog.cache_store, self.state.embeddings.store):
                    await run_blocking(cache.prune, limit=250)
                from .owner_cleanup import prune_orphan_scopes
                orphaned = await prune_orphan_scopes(self.state, at=now(), limit=100)
            from .call_cleanup import collect_call_orphans
            call_sweep = await run_blocking(collect_call_orphans, self.state.logs, limit=100)
            ready = await recover_cache_inventory(self.state, limit=250)
            deleted, batches, idle = call_sweep["deleted"] + orphaned, 0, 0
            by_kind = {"call_orphans": call_sweep["deleted"], "orphan_scopes": orphaned}
            bulk_rows = 0
            if governance is not None:
                # Give the large, cheap SQL backlog its own bounded lane; live
                # object stat/authority checks cannot starve retired postings.
                while (bulk_rows < row_budget // 2
                       and time.monotonic() - started < seconds / 2):
                    budget = min(5000, row_budget // 2 - bulk_rows)
                    count = await drain_retired_postings(self.state, at=now(), limit=budget)
                    deleted += count
                    bulk_rows += budget
                    by_kind["catalog_postings"] = by_kind.get("catalog_postings", 0) + count
                    if count < budget:
                        break
                    await asyncio.sleep(0)
            # At least one full rotation of catalog subdomains is needed before
            # an empty batch means there is no immediately reclaimable SQL work.
            while bulk_rows + batches * 250 < row_budget and time.monotonic() - started < seconds:
                result = await run_storage_retention(self.state, at=now(), batch_limit=250)
                batches += 1
                deleted += result["deleted"]
                for name, count in result["by_kind"].items():
                    by_kind[name] = by_kind.get(name, 0) + count
                idle = idle + 1 if result["deleted"] == 0 else 0
                if idle >= 5:
                    break
                await asyncio.sleep(0)
            self.last_result = {
                "status": "completed" if ready else "inventory_rebuilding",
                "at": now().isoformat(), "deleted": deleted, "by_kind": by_kind,
                "batches": batches, "released_reservations": released, "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
            self.failures = 0
            return self.last_result

    async def _run(self):
        await asyncio.sleep(5)
        while True:
            try:
                governance = getattr(self.state, "storage_governance", None)
                if governance is not None and time.monotonic() - self.policy_refreshed >= 5:
                    await governance.refresh()
                    self.policy_refreshed = time.monotonic()
                policy = governance.policy if governance is not None else {}
                result = await self.run_once(
                    seconds=policy.get("gc_cycle_milliseconds", 2000) / 1000,
                    row_budget=policy.get("gc_row_budget", 20000))
                delay = (.25 if result["status"] == "inventory_rebuilding" else 1
                         if governance is not None and not governance.files["complete"] else 5
                         if result.get("deleted") else policy.get("gc_interval_seconds", 30))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry cleanup without stopping the gateway
                self.failures = min(self.failures + 1, 5)
                self.last_result = {"status": "retry_pending", "at": now().isoformat(),
                                    "error_type": type(exc).__name__, "deleted": 0}
                delay = min(300, 5 * 2**self.failures)
            await asyncio.sleep(delay)

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="storage-governor")

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.owns_leader:
            await run_blocking(self.leader.release)
            self.owns_leader = False
