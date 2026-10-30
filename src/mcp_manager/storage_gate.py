"""Same-host admission: ordinary operations overlap; exclusive maintenance fails fast."""
import asyncio
import threading
from contextlib import ExitStack, asynccontextmanager, contextmanager
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
        # FileLock(thread_local=False) is reentrant, not a thread mutex. Serialize
        # its physical acquire/release and the worker count as one transition.
        with self.local:
            if self.closed:
                raise RuntimeError("Derived repository is closed")
            if not self.count:
                try:
                    with self.admission.acquire(poll_interval=0.001):
                        self.activity.acquire()
                except Timeout as exc:
                    from .runtime import GatewayError
                    raise GatewayError("storage_busy", "Storage maintenance is active; retry later") from exc
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
        # A separate instance prevents same-process reentrancy from bypassing
        # admission. ExitStack covers acquisition AND the body, including OS
        # failures while scanning workers. A busy probe releases before yielding.
        with ExitStack() as held:
            abandoned = []
            try:
                held.enter_context(FileLock(self.admission.lock_file, timeout=0, preserve_lock_file=True))
                for path in self.workers.glob("*.lease"):
                    held.enter_context(FileLock(str(path.with_suffix(".activity")),
                                                timeout=0, preserve_lock_file=True))
                    lease = FileLock(str(path), timeout=0, preserve_lock_file=True)
                    try:
                        with lease:
                            abandoned.extend([path, path.with_suffix(".activity")])
                    except Timeout:
                        pass
            except Timeout:
                held.close()
                yield False
                return
            try:
                yield True
            finally:
                held.close()
                for path in abandoned:
                    path.unlink(missing_ok=True)

    def close(self):
        with self.local, self.admission.acquire(poll_interval=0.001):
            if self.closed:
                return
            if self.count:
                raise RuntimeError("Cannot close a derived repository with active requests")
            self.closed = True
            self.lease.release()
            self.lease_path.unlink(missing_ok=True)
            self.activity_path.unlink(missing_ok=True)
