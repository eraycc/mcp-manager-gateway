"""Persistent jobs executed by a fixed, fair, bounded worker pool."""
import asyncio
import logging
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field

from .diagnostics import safe_diagnostic_error
from .paging import envelope_bytes
from .runtime import GatewayError

LOGGER = logging.getLogger(__name__)
MAX_QUEUED_ITEMS = 1000
MAX_QUEUE_BYTES = 16 * 1024 * 1024


@dataclass
class PendingJob:
    identity: str
    user_id: str | None
    operation: object
    cancel_operation: object
    remaining: int
    cancelled: bool = False
    active: dict = field(default_factory=dict)


class Jobs:
    def __init__(self, data_dir=None, *, store):
        self.store = store
        self.tasks = {}
        self.contexts = {}
        self.queues = OrderedDict()
        self.active_users = defaultdict(int)
        self.queued_items = 0
        self.queued_bytes = 0
        self.workers = []
        self.limit = asyncio.Semaphore(4)
        self.wakeup = asyncio.Event()
        self.closing = False

    async def start(self):
        await self.store.interrupt_unfinished()
        self.workers = [asyncio.create_task(self._worker(), name=f"mmg-job-worker-{i}") for i in range(4)]

    async def submit(self, action, items, operation, user_id=None, cancel_operation=None):
        if self.closing or not isinstance(items, list) or len(items) > MAX_QUEUED_ITEMS:
            raise GatewayError("busy", "Background work queue is full or closing")
        # Encode only bounded input, off the event loop; no Task per waiting item.
        sizes = await asyncio.to_thread(
            lambda: [envelope_bytes({"item": item}, max_bytes=MAX_QUEUE_BYTES) + 256 for item in items]
        )
        size = sum(sizes)
        if self.queued_items + len(items) > MAX_QUEUED_ITEMS or self.queued_bytes + size > MAX_QUEUE_BYTES:
            raise GatewayError("busy", "Background work queue exceeds its item or byte budget")
        self.queued_items += len(items)
        self.queued_bytes += size
        try:
            status = await self.store.create_job(action, len(items), user_id=user_id)
        except BaseException:
            self.queued_items -= len(items)
            self.queued_bytes -= size
            raise
        identity = status["id"]
        future = asyncio.get_running_loop().create_future()
        # Consume unobserved exceptions without suppressing them for explicit awaiters.
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        self.tasks[identity] = future
        self.contexts[identity] = PendingJob(identity, user_id, operation, cancel_operation, len(items))
        queue = self.queues.setdefault(user_id, deque())
        queue.extend((identity, ordinal, item, sizes[ordinal]) for ordinal, item in enumerate(items))
        if not items:
            await self._complete(identity)
        self.wakeup.set()
        return status

    def _next(self):
        for user_id in list(self.queues):
            queue = self.queues[user_id]
            if not queue:
                self.queues.pop(user_id)
                continue
            if self.active_users[user_id] >= 2:
                continue
            self.queues.move_to_end(user_id)
            identity, ordinal, item, size = queue.popleft()
            self.queued_items -= 1
            self.queued_bytes -= size
            context = self.contexts.get(identity)
            if context is None:
                return self._next()
            self.active_users[user_id] += 1
            return context, ordinal, item
        return None

    async def _record(self, context, ordinal, result):
        try:
            await self.store.append_result(context.identity, ordinal, result)
        except GatewayError as exc:
            if exc.code != "artifact_too_large":
                raise
            await self.store.append_result(context.identity, ordinal, {
                "item": str(ordinal), "ok": False, "outcome_unknown": bool(result.get("ok")),
                "error": "Result exceeds storage byte budget; operation was not replayed",
            })
        context.remaining -= 1
        if context.remaining == 0:
            await self._complete(context.identity)

    async def _complete(self, identity):
        context = self.contexts.get(identity)
        if context is None:
            return
        status = await self.store.get_status(identity)
        state = "cancelled" if context.cancelled else "completed_with_errors" if status["failed"] else "completed"
        status = await self.store.set_status(identity, state)
        self.contexts.pop(identity, None)
        future = self.tasks.pop(identity, None)
        if future is not None and not future.done():
            future.set_result(status)

    def _cancel_now(self, identity):
        context = self.contexts.get(identity)
        if context:
            context.cancelled = True
            for task in list(context.active.values()):
                if not task.done() and not task.cancelling():
                    task.cancel()
            self.wakeup.set()

    async def cancel(self, identity):
        self._cancel_now(identity)
        future = self.tasks.get(identity)
        if future is not None:
            await asyncio.shield(future)

    async def _worker(self):
        while True:
            selected = self._next()
            if selected is None:
                self.wakeup.clear()
                await self.wakeup.wait()
                continue
            context, ordinal, item = selected
            admitted = False
            try:
                await self.limit.acquire()
                admitted = True
                result = {"item": item, "ok": False, "cancelled": True, "error": "Operation cancelled"}
                if not context.cancelled:
                    await self.store.set_status(context.identity, "running")
                    # Cancellation can happen while the status transaction waits.
                    # Recheck before creating the side-effecting operation task.
                    if not context.cancelled:
                        child = asyncio.create_task(context.operation(item))
                        context.active[ordinal] = child
                        try:
                            value = await child
                            result = {"item": item, "ok": True, "result": value}
                        except asyncio.CancelledError:
                            self._cancel_now(context.identity)
                            result["outcome_unknown"] = True
                            if context.cancel_operation:
                                try:
                                    await context.cancel_operation(item)
                                except Exception as exc:  # noqa: BLE001 - cancellation callback is isolated
                                    LOGGER.warning("Cancellation callback failed: %s", type(exc).__name__)
                        except Exception as exc:  # noqa: BLE001 - one item must not fail its job
                            config = item if isinstance(item, dict) else {}
                            safe = safe_diagnostic_error(exc, config=config)
                            result = {"item": item, "ok": False, "error": safe["message"], "code": safe["code"],
                                      "outcome_unknown": safe["code"] == "outcome_unknown"}
                        finally:
                            context.active.pop(ordinal, None)
                await self._record(context, ordinal, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception("Job persistence failed: %s", type(exc).__name__)
                self._cancel_now(context.identity)
                try:
                    await self.store.set_status(context.identity, "failed", error="Persistence failed; operations were not replayed")
                finally:
                    self.contexts.pop(context.identity, None)
                    future = self.tasks.pop(context.identity, None)
                    if future is not None and not future.done():
                        future.set_exception(GatewayError("job_persistence_failed", "Could not persist job progress"))
            finally:
                if admitted:
                    self.limit.release()
                self.active_users[context.user_id] -= 1
                if self.active_users[context.user_id] == 0:
                    self.active_users.pop(context.user_id, None)
                self.wakeup.set()

    async def close(self):
        self.closing = True
        # Tests and failed startup may queue before start(); cancellation still
        # needs consumers to persist one terminal result for every item.
        if self.contexts and not self.workers:
            self.workers = [asyncio.create_task(self._worker(), name=f"mmg-job-close-{i}") for i in range(4)]
        pending = list(self.tasks.values())
        for identity in list(self.contexts):
            self._cancel_now(identity)
        for _worker in self.workers:
            self.limit.release()
        await asyncio.gather(*pending, return_exceptions=True)
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
