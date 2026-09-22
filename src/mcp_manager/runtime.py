"""A single owner for downstream connections, leases and lifecycle transitions."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from weakref import WeakValueDictionary

from .protocol_errors import is_request_error


def startup_failure_reason(exc):
    """Expose transport leaf errors rather than an opaque AnyIO task-group label."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(dict.fromkeys(startup_failure_reason(child) for child in exc.exceptions))
    return str(exc) or type(exc).__name__


class GatewayError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(message)


@dataclass(frozen=True)
class ServerSpec:
    id: str
    transport: str
    config: dict
    mode: str = "lazy"
    isolation: str = "service"
    revision: int = 1
    credential_scope: str = ""
    credential_owner: str = ""


@dataclass
class Lease:
    id: str
    user_id: str
    token_id: str
    ttl: float | None
    touched: float
    kind: str = "agent"


@dataclass
class Instance:
    key: tuple
    spec: ServerSpec
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: str = "starting"
    refs: set[str] = field(default_factory=set)
    in_flight: int = 0
    last_activity: float = 0
    connection: Any = None
    error: str | None = None
    failure: Exception | None = None
    owner: asyncio.Task | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


class Runtime:
    def __init__(self, connector, *, clock: Callable[[], float] = time.monotonic,
                 idle_seconds: float = 86400):
        self.connector = connector
        self.clock = clock
        self.idle_seconds = idle_seconds
        self.leases: dict[str, Lease] = {}
        self.instances: dict[tuple, Instance] = {}
        # Waiters keep strong references; inactive session/revision keys can disappear.
        self._locks: WeakValueDictionary[tuple, asyncio.Lock] = WeakValueDictionary()
        self.on_connect = None
        self.on_starting = None
        self.on_start_failure = None
        self._closing = False
        self.holds: set[str] = set()
        self.versions: dict[str, int] = {}
        self.credential_versions: dict[tuple, str | None] = {}

    def create_lease(self, user_id: str, token_id: str, *, ttl: float | None = None,
                     kind: str = "agent") -> Lease:
        if self._closing:
            raise GatewayError("shutting_down", "Gateway is shutting down")
        lease = Lease(uuid.uuid4().hex, user_id, token_id, ttl, self.clock(), kind)
        self.leases[lease.id] = lease
        return lease

    def check_lease(self, lease_id: str, user_id: str | None = None,
                    token_id: str | None = None) -> Lease:
        lease = self.leases.get(lease_id)
        if not lease or (lease.ttl is not None and self.clock() - lease.touched >= lease.ttl):
            raise GatewayError("lease_expired", "Lease expired; create a new lease")
        if user_id is not None and (lease.user_id != user_id or lease.token_id != token_id):
            raise GatewayError("forbidden", "Lease belongs to a different caller")
        return lease

    def heartbeat(self, lease_id: str, user_id: str, token_id: str) -> Lease:
        lease = self.check_lease(lease_id, user_id, token_id)
        lease.touched = self.clock()
        return lease

    def _key(self, spec: ServerSpec, lease: Lease):
        scope = lease.id if spec.isolation == "session" else (
            lease.user_id if spec.isolation == "user" else "service")
        return spec.id, spec.revision, spec.credential_scope, scope

    async def _owner(self, instance: Instance):
        """Enter and exit SDK/AnyIO transport contexts in this one task."""
        try:
            timeout = float(instance.spec.config.get("startup_timeout", 30))
            async with asyncio.timeout(timeout) as startup:
                if self.on_starting is not None:
                    await self.on_starting(instance.spec)
                async with self.connector(instance.spec) as connection:
                    instance.connection = connection
                    if self.on_connect is not None:
                        await self.on_connect(instance.spec, connection)
                    startup.reschedule(None)
                    if instance.phase == "starting":
                        instance.phase = "ready"
                    instance.ready.set()
                    await instance.stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = getattr(
                exc,
                "code",
                "startup_timeout" if isinstance(exc, TimeoutError) else "startup_failed",
            )
            failure = (
                exc
                if isinstance(exc, GatewayError)
                else GatewayError(code, startup_failure_reason(exc))
            )
            instance.error = str(failure)
            instance.failure = failure
            instance.phase = "failed"
            if self.on_start_failure is not None:
                try:
                    await self.on_start_failure(instance.spec, failure)
                except Exception as callback_error:
                    instance.error = startup_failure_reason(callback_error)
                    instance.failure = callback_error
        finally:
            instance.connection = None
            if instance.phase != "failed":
                instance.phase = "stopped"
            instance.ready.set()
            instance.closed.set()
            if self.instances.get(instance.key) is instance:
                self.instances.pop(instance.key, None)

    def _available(self, spec):
        if self._closing:
            raise GatewayError("shutting_down", "Gateway is shutting down")
        if spec.mode == "disabled":
            raise GatewayError("disabled", "MCP is disabled")
        if spec.id in self.holds:
            raise GatewayError("stopped", "MCP was manually stopped; start it before calling")
        if spec.credential_owner and self.credential_versions.get(
                (spec.id, spec.credential_owner), spec.credential_scope) != spec.credential_scope:
            raise GatewayError("credential_changed", "OAuth credentials changed; call again with current authorization")
        if self.versions.get(spec.id, spec.revision) != spec.revision:
            raise GatewayError("revision_changed", "MCP configuration changed; refresh the directory")

    async def _acquire(self, spec: ServerSpec, lease_id: str) -> Instance:
        self._available(spec)
        lease = self.check_lease(lease_id)
        key = self._key(spec, lease)
        lock = self._locks.setdefault(key, asyncio.Lock())
        while True:
            wait_closed = None
            async with lock:
                self._available(spec)
                self.check_lease(lease_id)
                other = next((i for k, i in self.instances.items() if k != key and k[0] == key[0]
                              and k[-1] == key[-1] and not i.closed.is_set()), None)
                if other:
                    if other.phase not in {"draining", "stopping"}:
                        other.phase = "draining"
                        asyncio.create_task(self._stop_instance(other))
                    wait_closed = other.closed
                instance = self.instances.get(key)
                if wait_closed:
                    pass
                elif instance and instance.phase in ("draining", "stopping"):
                    wait_closed = instance.closed
                else:
                    if instance is None or instance.closed.is_set():
                        instance = Instance(key, spec, last_activity=self.clock())
                        instance.drained.set()
                        concurrency = max(1, min(256, int(spec.config.get("concurrency", 1))))
                        instance.semaphore = asyncio.Semaphore(concurrency)
                        self.instances[key] = instance
                        instance.owner = asyncio.create_task(self._owner(instance))
                    instance.refs.add(lease_id)
            if wait_closed:
                await wait_closed.wait()
                self.check_lease(lease_id)
                continue
            try:
                timeout = float(spec.config.get("startup_timeout", 30))
                await asyncio.wait_for(asyncio.shield(instance.ready.wait()), timeout)
            except TimeoutError as exc:
                # Caller detaches; the owner has an independent bounded startup timeout
                # in the transport adapter. Do not cancel another waiter's startup.
                instance.refs.discard(lease_id)
                raise GatewayError("startup_timeout", "MCP startup timed out") from exc
            if instance.phase == "failed":
                if isinstance(instance.failure, GatewayError):
                    raise instance.failure
                raise GatewayError("startup_failed", instance.error or "MCP startup failed") from instance.failure
            async with lock:
                self.check_lease(lease_id)
                if instance.phase != "ready":
                    continue
                instance.in_flight += 1
                instance.drained.clear()
                return instance

    async def perform(self, spec: ServerSpec, lease_id: str, operation: str,
                      *args, authorize=None, business=True):
        if authorize is not None:
            await authorize()
        instance = await self._acquire(spec, lease_id)
        dispatched = False
        try:
            queue_timeout = float(spec.config.get("queue_timeout", 60))
            try:
                await asyncio.wait_for(instance.semaphore.acquire(), queue_timeout)
            except TimeoutError as exc:
                raise GatewayError("queue_timeout", "MCP call queue timed out") from exc
            try:
                self.check_lease(lease_id)
                if authorize is not None:
                    await authorize()
                self._available(spec)
                if instance.phase != "ready":
                    raise GatewayError("stopped", "MCP is draining or stopped")
                if business:
                    instance.last_activity = self.clock()
                dispatched = True
                method = getattr(instance.connection, operation)
                timeout = float(spec.config.get("call_timeout", 60))
                return await asyncio.wait_for(method(*args), timeout)
            finally:
                instance.semaphore.release()
        except GatewayError as exc:
            if exc.code in {"permission_revoked", "auth_required", "credential_changed", "forbidden", "lease_expired"}:
                instance.refs.discard(lease_id)
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not dispatched:
                instance.refs.discard(lease_id)
                raise
            if is_request_error(exc):
                raise GatewayError("downstream_error", str(exc)) from exc
            instance.phase = "draining"
            instance.refs.clear()
            code = "outcome_unknown" if dispatched and business else "connection_error"
            raise GatewayError(code, str(exc) or "Downstream did not return a result") from exc
        finally:
            if business and dispatched:
                instance.last_activity = self.clock()
            instance.in_flight -= 1
            if instance.in_flight == 0:
                instance.drained.set()
                if instance.phase == "draining":
                    await self._stop_instance(instance)
                elif not instance.refs and (instance.spec.mode == "lazy" or instance.spec.isolation == "session"):
                    await self._stop_instance(instance, only_unreferenced=True)

    async def call(self, spec, lease_id, name, arguments, *, authorize=None):
        return await self.perform(spec, lease_id, "call", name, arguments, authorize=authorize)

    async def discover(self, spec, lease_id):
        return await self.perform(spec, lease_id, "discover", business=False)

    def mark_available(self, spec: ServerSpec):
        """Reject unavailable revisions through the public runtime boundary."""
        self._available(spec)

    async def stop_instance(self, instance: Instance, *, force=False, only_unreferenced=False):
        """Stop one runtime instance without exposing lifecycle internals."""
        await self._stop_instance(
            instance, force=force, only_unreferenced=only_unreferenced
        )

    async def _stop_instance(self, instance: Instance, *, force=False, only_unreferenced=False):
        lock = self._locks.setdefault(instance.key, asyncio.Lock())
        async with lock:
            if instance.closed.is_set() or (only_unreferenced and instance.refs):
                return
            instance.phase = "draining"
        timeout = float(instance.spec.config.get("stop_timeout", 10))
        try:
            await asyncio.wait_for(instance.drained.wait(), timeout)
        except TimeoutError:
            if not force:
                # Keep draining. A later call completion / reap will finish the stop.
                return
        instance.phase = "stopping"
        instance.stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(instance.closed.wait()), timeout)
        except TimeoutError:
            if instance.owner:
                instance.owner.cancel()
                await asyncio.gather(instance.owner, return_exceptions=True)

    async def release(self, lease_id: str, *, keep_alive=False):
        self.leases.pop(lease_id, None)
        for instance in list(self.instances.values()):
            if lease_id not in instance.refs:
                continue
            instance.refs.discard(lease_id)
            if not keep_alive and not instance.refs and (instance.spec.mode == "lazy" or instance.spec.isolation == "session"):
                await self._stop_instance(instance, only_unreferenced=True)

    async def stop_server(self, server_id: str, *, hold=False, force=False, require_idle=False, revision=None):
        instances = [i for i in self.instances.values() if i.spec.id == server_id
                     and (revision is None or i.spec.revision == revision)]
        # No await before admission is blocked: a rejected edit leaves the old
        # generation and any pre-existing manual hold untouched.
        if require_idle and any(i.in_flight or i.phase == "starting" for i in instances):
            raise GatewayError("busy", "MCP has active calls or is starting; retry after completion")
        if hold:
            self.holds.add(server_id)
        for instance in instances:
            instance.refs.clear()
            await self._stop_instance(instance, force=force)

    async def invalidate_credential(self, server_id, owner):
        self.credential_versions[(server_id, owner)] = None
        for instance in list(self.instances.values()):
            if instance.spec.id == server_id and instance.spec.credential_owner == owner:
                instance.refs.clear()
                await self._stop_instance(instance)

    async def restrict_lease(self, lease_id, allowed):
        for instance in list(self.instances.values()):
            if lease_id in instance.refs and instance.spec.id not in allowed:
                instance.refs.discard(lease_id)
                if not instance.refs and (instance.spec.mode == "lazy" or instance.spec.isolation == "session"):
                    await self._stop_instance(instance, only_unreferenced=True)

    async def revoke(self, *, user_id: str | None = None, token_id: str | None = None):
        owned = []
        if user_id is not None and token_id is None:
            # Eager personal connections outlive their maintenance leases.
            owned = [i for i in self.instances.values()
                     if i.spec.credential_owner == user_id
                     or (i.spec.isolation == "user" and i.key[-1] == user_id)]
            for instance in owned:
                if instance.spec.credential_owner == user_id:
                    self.credential_versions[(instance.spec.id, user_id)] = None
        for lease in list(self.leases.values()):
            if (user_id is None or lease.user_id == user_id) and (
                    token_id is None or lease.token_id == token_id):
                await self.release(lease.id)
        for instance in owned:
            instance.refs.clear()
            await self._stop_instance(instance)

    async def reap(self):
        now = self.clock()
        for lease in list(self.leases.values()):
            if lease.ttl is not None and now - lease.touched >= lease.ttl:
                await self.release(lease.id)
        for instance in list(self.instances.values()):
            idle = float(instance.spec.config.get("idle_seconds", self.idle_seconds))
            if instance.phase == "draining" and instance.in_flight == 0:
                await self._stop_instance(instance)
            elif (instance.spec.mode == "lazy" and instance.in_flight == 0
                  and instance.phase == "ready" and idle > 0
                  and now - instance.last_activity >= idle):
                instance.refs.clear()
                await self._stop_instance(instance)
        # Unidentified HTTP callers have retention leases rather than precise Agent sessions.
        referenced = set().union(*(i.refs for i in self.instances.values())) if self.instances else set()
        for lease in list(self.leases.values()):
            if lease.kind == "retention" and lease.id not in referenced:
                self.leases.pop(lease.id, None)

    def status(self, *, user_id: str | None = None):
        result = []
        for instance in self.instances.values():
            visible = [self.leases[l] for l in instance.refs if l in self.leases]
            if user_id is not None:
                shared = (instance.spec.isolation == "service"
                          and instance.spec.credential_owner in {"", "service"})
                owned = (instance.spec.credential_owner == user_id
                         or (instance.spec.isolation == "user" and instance.key[-1] == user_id))
                if not shared and not owned and not any(l.user_id == user_id for l in visible):
                    continue
            result.append({"server_id": instance.spec.id, "generation": instance.generation,
                           "phase": instance.phase, "lease_count": len(instance.refs),
                           "in_flight": instance.in_flight, "last_error": instance.error,
                           "idle_seconds": max(0, self.clock() - instance.last_activity)})
        return result

    async def close(self):
        self._closing = True
        self.leases.clear()
        await asyncio.gather(*(self._stop_instance(i, force=True)
                               for i in list(self.instances.values())))
