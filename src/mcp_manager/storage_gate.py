"""Same-host admission: ordinary operations overlap; exclusive maintenance fails fast."""
import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

from .storage_async import join_task, run_blocking


class StorageGate:
    def __init__(self, data_dir):
        root = Path(data_dir).resolve() / "locks"
        root.mkdir(parents=True, exist_ok=True)
        self.workers = root / "derived-workers"
        self.workers.mkdir(exist_ok=True)
        self.admission = FileLock(str(root / "derived-admission.lock"), timeout=5, thread_local=False, preserve_lock_file=True)
        identity = uuid4().hex
        self.lease_path = self.workers / (identity + ".lease")
        self.activity_path = self.workers / (identity + ".activity")
        self.lease = FileLock(str(self.lease_path), timeout=0, thread_local=False, preserve_lock_file=True)
        self.activity = FileLock(str(self.activity_path), timeout=5, thread_local=False, preserve_lock_file=True)
        self.local = threading.RLock()
        self.count, self.closed = 0, False
        with self.admission.acquire(poll_interval=0.001):
            self.lease.acquire()

    def _enter(self):
        # An active local operation already holds this worker's activity lock.
        # A maintainer therefore cannot enter; nested/concurrent local work can
        # join it without reopening the global admission file on every call.
        with self.local:
            if self.closed:
                raise RuntimeError("Derived repository is closed")
            if self.count:
                self.count += 1
                return
        with self.admission.acquire(poll_interval=0.001), self.local:
            if self.closed:
                raise RuntimeError("Derived repository is closed")
            if not self.count:
                self.activity.acquire()
            self.count += 1

    def _exit(self):
        with self.local:
            self.count -= 1
            if not self.count:
                self.activity.release()

    @contextmanager
    def operation(self):
        self._enter()
        try:
            yield
        finally:
            self._exit()

    @asynccontextmanager
    async def async_operation(self):
        waiting = asyncio.create_task(asyncio.to_thread(self._enter))
        try:
            await asyncio.shield(waiting)
        except asyncio.CancelledError:
            try:
                await join_task(waiting)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001,S110 - keep owner cancellation after joining
                pass
            else:
                await run_blocking(self._exit)
            raise
        try:
            yield
        finally:
            await run_blocking(self._exit)

    @contextmanager
    def maintenance(self):
        # Never wait on active requests while holding the admission fence.
        admitted, held, abandoned = False, [], []
        try:
            self.admission.acquire(timeout=0)
            held.append(self.admission)
            for path in self.workers.glob("*.lease"):
                guard = FileLock(str(path.with_suffix(".activity")), timeout=0, thread_local=False, preserve_lock_file=True)
                guard.acquire()
                held.append(guard)
                lease = FileLock(str(path), timeout=0, thread_local=False, preserve_lock_file=True)
                try:
                    lease.acquire()
                except Timeout:
                    pass
                else:
                    lease.release()
                    abandoned.extend([path, path.with_suffix(".activity")])
            admitted = True
        except Timeout:
            pass
        try:
            yield admitted
        finally:
            for guard in reversed(held):
                guard.release()
            for path in abandoned:
                path.unlink(missing_ok=True)

    def close(self):
        with self.admission.acquire(poll_interval=0.001), self.local:
            if self.closed:
                return
            if self.count:
                raise RuntimeError("Cannot close a derived repository with active requests")
            self.closed = True
            self.lease.release()
            self.lease_path.unlink(missing_ok=True)
            self.activity_path.unlink(missing_ok=True)
