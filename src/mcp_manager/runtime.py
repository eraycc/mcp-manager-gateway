"""A single owner for downstream connections, leases and lifecycle transitions."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakValueDictionary

from .protocol_errors import is_request_error

LOGGER = logging.getLogger(__name__)


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
class RuntimeCapacity:
    global_calls: int = 64
    user_calls: int = 16
    mcp_calls: int = 16
    queued_calls: int = 256
    global_instances: int = 64
    user_instances: int = 16
    per_instance_waiters: int = 64
    queue_timeout: float = 60.0

    def __post_init__(self):
        limits = {
            "global_calls": 256,
            "user_calls": 64,
            "mcp_calls": 64,
            "queued_calls": 1024,
            "global_instances": 128,
            "user_instances": 32,
            "per_instance_waiters": 128,
        }
        for name, hard_limit in limits.items():
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(name + " must be a positive integer")
            object.__setattr__(self, name, min(value, hard_limit))
        if not isinstance(self.queue_timeout, (int, float)) or not 0 < self.queue_timeout <= 300:
            raise ValueError("queue_timeout must be greater than 0 and at most 300 seconds")
        object.__setattr__(self, "queue_timeout", float(self.queue_timeout))


@dataclass
class _CallWaiter:
    user_id: str
    mcp_id: str
    future: asyncio.Future
    queued_at: float
    granted: bool = False


class _FairCallGate:
    def __init__(self, capacity: RuntimeCapacity, clock):
        self.capacity = capacity
        self.clock = clock
        self.lock = asyncio.Lock()
        self.active = 0
        self.active_users = Counter()
        self.active_mcps = Counter()
        self.queues = {}
        self.order = deque()
        self.queued = 0
        self.last_queue_delay_ms = 0.0
        self.max_queue_delay_ms = 0.0

    def _can_grant(self, user_id, mcp_id):
        return (
            self.active < self.capacity.global_calls
            and self.active_users[user_id] < self.capacity.user_calls
            and self.active_mcps[mcp_id] < self.capacity.mcp_calls
        )

    def _activate(self, user_id, mcp_id):
        self.active += 1
        self.active_users[user_id] += 1
        self.active_mcps[mcp_id] += 1

    def _deactivate(self, user_id, mcp_id):
        self.active -= 1
        self.active_users[user_id] -= 1
        self.active_mcps[mcp_id] -= 1
        if not self.active_users[user_id]:
            del self.active_users[user_id]
        if not self.active_mcps[mcp_id]:
            del self.active_mcps[mcp_id]

    def _pump(self):
        while self.order and self.active < self.capacity.global_calls:
            progress = False
            for _ in range(len(self.order)):
                mcp_id = self.order.popleft()
                queue = self.queues.get(mcp_id)
                if not queue:
                    self.queues.pop(mcp_id, None)
                    continue
                chosen = None
                for _ in range(len(queue)):
                    waiter = queue[0]
                    if waiter.future.cancelled():
                        queue.popleft()
                        self.queued -= 1
                        continue
                    if self._can_grant(waiter.user_id, mcp_id):
                        chosen = queue.popleft()
                        break
                    queue.rotate(-1)
                if queue:
                    self.order.append(mcp_id)
                else:
                    self.queues.pop(mcp_id, None)
                if chosen is None:
                    continue
                self.queued -= 1
                chosen.granted = True
                self._activate(chosen.user_id, chosen.mcp_id)
                delay = max(0.0, (self.clock() - chosen.queued_at) * 1000)
                self.last_queue_delay_ms = delay
                self.max_queue_delay_ms = max(self.max_queue_delay_ms, delay)
                if not chosen.future.done():
                    chosen.future.set_result(None)
                progress = True
            if not progress:
                break

    async def acquire(self, user_id: str, mcp_id: str, timeout: float):
        loop = asyncio.get_running_loop()
        waiter = None
        async with self.lock:
            if self._can_grant(user_id, mcp_id):
                self._activate(user_id, mcp_id)
                return
            if self.queued >= self.capacity.queued_calls:
                raise GatewayError("busy", "Gateway call queue is full; retry later")
            queue = self.queues.setdefault(mcp_id, deque())
            waiter = _CallWaiter(user_id, mcp_id, loop.create_future(), self.clock())
            queue.append(waiter)
            self.queued += 1
            if mcp_id not in self.order:
                self.order.append(mcp_id)
            self._pump()
        try:
            await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
        except BaseException as exc:
            async with self.lock:
                if waiter.granted:
                    self._deactivate(user_id, mcp_id)
                else:
                    queue = self.queues.get(mcp_id)
                    if queue and waiter in queue:
                        queue.remove(waiter)
                        self.queued -= 1
                        if not queue:
                            self.queues.pop(mcp_id, None)
                            with contextlib.suppress(ValueError):
                                self.order.remove(mcp_id)
                if not waiter.future.done():
                    waiter.future.cancel()
                self._pump()
            if isinstance(exc, TimeoutError):
                raise GatewayError(
                    "busy",
                    "Gateway call queue timed out; retry later",
                ) from exc
            raise

    async def release(self, user_id: str, mcp_id: str):
        async with self.lock:
            self._deactivate(user_id, mcp_id)
            self._pump()

    def status(self):
        return {
            "active_business_calls": self.active,
            "queued_business_calls": self.queued,
            "active_calls_by_user": dict(self.active_users),
            "active_calls_by_mcp": dict(self.active_mcps),
            "queue_delay_ms_last": round(self.last_queue_delay_ms, 3),
            "queue_delay_ms_max": round(self.max_queue_delay_ms, 3),
        }


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
    credential_epoch: int | None = None
    name: str = ""


@dataclass
class Lease:
    id: str
    user_id: str
    token_id: str
    ttl: float | None
    touched: float
    kind: str = "agent"
    created: float = field(default_factory=time.time)
    in_flight: int = 0
    created_clock: float = 0
    pending: int = 0
    call_count: int = 0
    last_call_tick: float | None = None
    heartbeat_tick: float = 0
    heartbeat_seconds: int = 90
    missed_heartbeats: int = 3


@dataclass
class Instance:
    key: tuple
    spec: ServerSpec
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: str = "starting"
    refs: set[str] = field(default_factory=set)
    in_flight: int = 0
    waiters: int = 0
    admitted_user: str = ""
    admitted_token: str = ""
    last_activity: float = 0
    connection: Any = None
    error: str | None = None
    failure: Exception | None = None
    owner: asyncio.Task | None = None
    stop_task: asyncio.Task | None = None
    force_stop: bool = False
    stop_context: dict = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


class Runtime:
    def __init__(
        self,
        connector,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_seconds: float = 86400,
        connection_ttl_seconds: int = 43200,
        bridge_heartbeat_seconds: int = 90,
        bridge_missed_heartbeats: int = 3,
        capacity: RuntimeCapacity | None = None,
    ):
        self.connector = connector
        self.clock = clock
        self.idle_seconds = idle_seconds
        self.connection_ttl_seconds = connection_ttl_seconds
        self.bridge_heartbeat_seconds = bridge_heartbeat_seconds
        self.bridge_missed_heartbeats = bridge_missed_heartbeats
        self.capacity = capacity or RuntimeCapacity()
        self._business_gate = _FairCallGate(self.capacity, clock)
        self._instance_lock = asyncio.Lock()
        self.leases: dict[str, Lease] = {}
        self.instances: dict[tuple, Instance] = {}
        # Waiters keep strong references; inactive session/revision keys can disappear.
        self._locks: WeakValueDictionary[tuple, asyncio.Lock] = WeakValueDictionary()
        self.on_connect = None
        self.on_starting = None
        self.on_start_failure = None
        self.on_auth_failure = None
        self.on_lifecycle = None
        self._closing = False
        self.versions: dict[str, int] = {}
        self.credential_versions: dict[tuple, str | None] = {}

    async def _emit_lifecycle(self, action, details, *, target_type, target_id, target_name=None, result="success"):
        if self.on_lifecycle is None:
            return
        try:
            await self.on_lifecycle(action, None, details, source="system", actor_type="system",
                                    username=None, ip=None, request_id=None, target_type=target_type,
                                    target_id=target_id, target_name=target_name, result=result)
        except Exception as exc:  # noqa: BLE001 - auditing must never prevent lifecycle cleanup
            LOGGER.warning("Lifecycle audit failed for %s: %s", action, type(exc).__name__)

    async def _emit_stop(self, instance):
        context = dict(instance.stop_context)
        reason = context.pop("reason_code", "connection_error" if instance.failure else "stop_requested")
        # The control-plane operation already audits an explicit manual stop.
        if reason == "manual_stop":
            return
        details = {
            "mcp_id": instance.spec.id, "instance_id": instance.generation,
            "mode": instance.spec.mode, "isolation": instance.spec.isolation,
            "revision": instance.spec.revision,
            "owner_id": instance.spec.credential_owner or (
                "service" if instance.spec.isolation == "service" else instance.admitted_user),
            "reason_code": reason, "force": instance.force_stop,
            "lease_count": len(instance.refs & self.leases.keys()), **context,
        }
        if instance.failure:
            details.update(error_type=type(instance.failure).__name__,
                           error_code=getattr(instance.failure, "code", "connection_error"))
        await self._emit_lifecycle("mcp.stop", details, target_type="mcp", target_id=instance.spec.id,
                                   target_name=instance.spec.name or None,
                                   result="failure" if instance.failure else "success")

    def create_lease(self, user_id: str, token_id: str, *, ttl: float | None = None,
                     kind: str = "agent") -> Lease:
        if self._closing:
            raise GatewayError("shutting_down", "Gateway is shutting down")
        tick = self.clock()
        lease = Lease(uuid.uuid4().hex, user_id, token_id, ttl, tick, kind,
                      created=time.time(), created_clock=tick, heartbeat_tick=tick,
                      heartbeat_seconds=self.bridge_heartbeat_seconds,
                      missed_heartbeats=self.bridge_missed_heartbeats)
        self.leases[lease.id] = lease
        return lease

    def lease_timeout(self, lease: Lease):
        if lease.kind in {"agent", "bridge", "retention"}:
            return self.connection_ttl_seconds or None
        return lease.ttl

    def bridge_heartbeat_policy(self, lease: Lease):
        if lease.kind != "bridge" or self.bridge_missed_heartbeats == 0:
            return 0, 0
        # On re-enable, allow three existing heartbeat intervals until the client
        # receives the new limit, without exempting disconnected clients forever.
        return lease.heartbeat_seconds or 90, lease.missed_heartbeats or 3

    def lease_expired(self, lease: Lease, *, now=None):
        ttl = self.lease_timeout(lease)
        tick = self.clock() if now is None else now
        inactive = ttl is not None and tick - lease.touched >= ttl
        interval, missed_heartbeats = self.bridge_heartbeat_policy(lease)
        lost_bridge = interval > 0 and tick - lease.heartbeat_tick >= interval * missed_heartbeats
        return not lease.in_flight and not lease.pending and (inactive or lost_bridge)

    def check_lease(self, lease_id: str, user_id: str | None = None,
                    token_id: str | None = None) -> Lease:
        lease = self.leases.get(lease_id)
        if not lease or self.lease_expired(lease):
            raise GatewayError("lease_expired", "Lease expired; create a new lease")
        if user_id is not None and (lease.user_id != user_id or lease.token_id != token_id):
            raise GatewayError("forbidden", "Lease belongs to a different caller")
        return lease

    def heartbeat(self, lease_id: str, user_id: str, token_id: str) -> Lease:
        lease = self.check_lease(lease_id, user_id, token_id)
        lease.touched = lease.heartbeat_tick = self.clock()
        # Switch only when the client receives the new policy; a shorter server
        # default must not expire a healthy client still sleeping on its old interval.
        lease.heartbeat_seconds = self.bridge_heartbeat_seconds
        lease.missed_heartbeats = self.bridge_missed_heartbeats
        return lease

    def _key(self, spec: ServerSpec, lease: Lease):
        scope = lease.id if spec.isolation == "session" else (
            lease.user_id if spec.isolation == "user" else "service")
        return spec.id, spec.revision, spec.credential_scope, scope

    async def _owner(self, instance: Instance):
        """Enter and exit SDK/AnyIO transport contexts in this one task."""
        was_ready = False
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
                    was_ready = True
                    instance.ready.set()
                    await instance.stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- transport boundary normalizes plugin failures.
            if (self.on_auth_failure is not None and instance.spec.config.get("auth", {}).get("type") == "oauth"
                    and oauth_http_401(exc)):
                await self.on_auth_failure(instance.spec)
                exc = GatewayError("auth_required", "授权已失效，请重新授权；本次不会自动重放调用")
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
            from .diagnostics import safe_gateway_error

            failure = safe_gateway_error(failure, config=instance.spec.config)
            instance.error = str(failure)
            instance.failure = failure
            instance.phase = "failed"
            if self.on_start_failure is not None:
                try:
                    await self.on_start_failure(instance.spec, failure)
                except Exception as callback_error:  # noqa: BLE001 -- callback errors are sanitized.
                    callback_error = safe_gateway_error(callback_error, config=instance.spec.config)
                    instance.error = str(callback_error)
                    instance.failure = callback_error
        finally:
            instance.connection = None
            if instance.phase != "failed":
                instance.phase = "stopped"
            instance.ready.set()
            try:
                if was_ready:
                    await self._emit_stop(instance)
            finally:
                instance.closed.set()
                async with self._instance_lock:
                    if self.instances.get(instance.key) is instance:
                        self.instances.pop(instance.key, None)

    def _available(self, spec):
        if self._closing:
            raise GatewayError("shutting_down", "Gateway is shutting down")
        if spec.mode == "disabled":
            raise GatewayError("disabled", "MCP is disabled")
        if spec.credential_owner and self.credential_versions.get(
                (spec.id, spec.credential_owner), spec.credential_scope) != spec.credential_scope:
            raise GatewayError("credential_changed", "OAuth credentials changed; call again with current authorization")
        if self.versions.get(spec.id, spec.revision) != spec.revision:
            raise GatewayError("revision_changed", "MCP configuration changed; refresh the directory")

    async def _make_instance_room(self, user_id: str):
        """Evict one idle LRU connection when bounded instance slots are full."""
        victim = None
        async with self._instance_lock:
            active = [item for item in self.instances.values() if not item.closed.is_set()]
            owned = [item for item in active if item.admitted_user == user_id]
            if (
                len(active) < self.capacity.global_instances
                and len(owned) < self.capacity.user_instances
            ):
                return
            pool = owned if len(owned) >= self.capacity.user_instances else active
            idle = [item for item in pool if item.phase == "ready" and item.in_flight == 0
                    and item.spec.mode == "lazy"]
            if idle:
                victim = min(idle, key=lambda item: item.last_activity)
        if victim is None:
            raise GatewayError("busy", "Runtime instance capacity is full; retry later")
        await self._stop_instance(victim, idle_seconds=0, reason="capacity_eviction",
                                  expected_activity=victim.last_activity, clear_refs=True)

    async def _acquire(self, spec: ServerSpec, lease_id: str) -> Instance:
        self._available(spec)
        lease = self.check_lease(lease_id)
        key = self._key(spec, lease)
        current = self.instances.get(key)
        if current is None or current.closed.is_set():
            await self._make_instance_room(lease.user_id)
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
                        asyncio.create_task(self._stop_instance(other, reason=(
                            "credential_changed" if other.spec.revision == spec.revision else "configuration_changed")))
                    wait_closed = other.closed
                instance = self.instances.get(key)
                if wait_closed:
                    pass
                elif instance and instance.phase in ("draining", "stopping"):
                    wait_closed = instance.closed
                else:
                    if instance is None or instance.closed.is_set():
                        async with self._instance_lock:
                            active = [
                                item for item in self.instances.values()
                                if not item.closed.is_set()
                            ]
                            owned = sum(
                                item.admitted_user == lease.user_id
                                for item in active
                            )
                            if (
                                len(active) >= self.capacity.global_instances
                                or owned >= self.capacity.user_instances
                            ):
                                raise GatewayError(
                                    "busy",
                                    "Runtime instance capacity is full; retry later",
                                )
                            instance = Instance(
                                key,
                                spec,
                                admitted_user=lease.user_id,
                                admitted_token=lease.token_id,
                                last_activity=self.clock(),
                            )
                            instance.drained.set()
                            concurrency = max(
                                1,
                                min(256, int(spec.config.get("concurrency", 1))),
                            )
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
                if instance.waiters >= self.capacity.per_instance_waiters:
                    raise GatewayError("busy", "MCP instance wait queue is full; retry later")
                instance.in_flight += 1
                instance.waiters += 1
                instance.drained.clear()
                return instance

    async def perform(self, spec: ServerSpec, lease_id: str, operation: str,
                      *args, authorize=None, business=True):
        if authorize is not None:
            await authorize()
        lease = self.check_lease(lease_id)
        permit = None
        instance = None
        waiting = False
        dispatched = False
        acquired = False
        queue_timeout = min(
            float(spec.config.get("queue_timeout", self.capacity.queue_timeout)),
            self.capacity.queue_timeout,
        )
        if business:
            lease.touched = self.clock()
            lease.pending += 1
        try:
            if business:
                await self._business_gate.acquire(lease.user_id, spec.id, queue_timeout)
                permit = (lease.user_id, spec.id)
            self.check_lease(lease_id)
            if authorize is not None:
                await authorize()
            self._available(spec)
            instance = await self._acquire(spec, lease_id)
            waiting = True
            try:
                await asyncio.wait_for(instance.semaphore.acquire(), queue_timeout)
                acquired = True
                instance.waiters -= 1
                waiting = False
            except TimeoutError as exc:
                raise GatewayError("queue_timeout", "MCP call queue timed out") from exc
            self.check_lease(lease_id)
            if authorize is not None:
                await authorize()
            self._available(spec)
            if instance.phase != "ready":
                raise GatewayError("stopped", "MCP is draining or stopped")
            if business:
                lease.touched = instance.last_activity = self.clock()
            dispatched = True
            lease.in_flight += 1
            method = getattr(instance.connection, operation, None)
            if method is None and operation == "ingest_catalog":
                from .transports import DiscoveryBudget, ingest_capabilities

                async def method(store, scope, operation_authorize=None):
                    return await ingest_capabilities(
                        instance.connection, store, scope,
                        budget=DiscoveryBudget.from_config(spec.config),
                        authorize=operation_authorize,
                    )
            if method is None:
                raise GatewayError("unsupported_operation", f"MCP does not support {operation}")
            timeout = float(spec.config.get("call_timeout", 60))
            if business:
                lease.call_count += 1
                lease.last_call_tick = self.clock()
            return await asyncio.wait_for(method(*args), timeout)
        except GatewayError as exc:
            if (dispatched and self.on_auth_failure is not None and spec.config.get("auth", {}).get("type") == "oauth"
                    and oauth_http_401(exc)):
                await self.on_auth_failure(spec)
            if (
                instance is not None
                and exc.code in {
                    "permission_revoked",
                    "auth_required",
                    "credential_changed",
                    "forbidden",
                    "lease_expired",
                }
            ):
                instance.refs.discard(lease_id)
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if (self.on_auth_failure is not None and spec.config.get("auth", {}).get("type") == "oauth"
                    and oauth_http_401(exc)):
                await self.on_auth_failure(spec)
                raise GatewayError("auth_required", "授权已失效，请重新授权；本次不会自动重放调用") from exc
            if not dispatched:
                if instance is not None:
                    instance.refs.discard(lease_id)
                raise
            if is_request_error(exc):
                raise GatewayError("downstream_error", str(exc)) from exc
            lock = self._locks.setdefault(instance.key, asyncio.Lock())
            async with lock:
                instance.phase = "draining"
                if not instance.stop_context:
                    instance.stop_context = {"reason_code": "connection_error", "error_type": type(exc).__name__}
                instance.refs.clear()
            code = "outcome_unknown" if business else "connection_error"
            raise GatewayError(code, str(exc) or "Downstream did not return a result") from exc
        finally:
            if business:
                lease.pending -= 1
                if dispatched:
                    lease.touched = self.clock()
            if dispatched:
                lease.in_flight -= 1
            if acquired and instance is not None:
                instance.semaphore.release()
            if waiting and instance is not None:
                instance.waiters -= 1
            if instance is not None:
                lock = self._locks.setdefault(instance.key, asyncio.Lock())
                async with lock:
                    if business and dispatched:
                        instance.last_activity = self.clock()
                    instance.in_flight -= 1
                    if instance.in_flight == 0:
                        instance.drained.set()
                    stopping = instance.phase == "draining" and instance.in_flight == 0
                    unreferenced = (not instance.refs and instance.spec.mode == "lazy"
                                    and instance.in_flight == 0)
                if stopping:
                    await self._stop_instance(instance)
                elif unreferenced:
                    await self._stop_instance(instance, only_unreferenced=True)
            if permit is not None:
                await self._business_gate.release(*permit)

    async def call(self, spec, lease_id, name, arguments, *, authorize=None):
        return await self.perform(spec, lease_id, "call", name, arguments, authorize=authorize)

    async def discover(self, spec, lease_id):
        return await self.perform(spec, lease_id, "discover", business=False)

    def mark_available(self, spec: ServerSpec):
        """Reject unavailable revisions through the public runtime boundary."""
        self._available(spec)

    async def stop_instance(self, instance: Instance, *, force=False, only_unreferenced=False, clear_refs=False,
                            reason=None):
        """Stop one runtime instance without exposing lifecycle internals."""
        await self._stop_instance(
            instance, force=force, only_unreferenced=only_unreferenced, clear_refs=clear_refs, reason=reason
        )

    async def _stop_instance(self, instance: Instance, *, force=False, only_unreferenced=False,
                             idle_seconds=None, expected_activity=None, clear_refs=False,
                             require_idle=False, reason=None, lease_id=None, operation_id=None):
        lock = self._locks.setdefault(instance.key, asyncio.Lock())
        async with lock:
            if (instance.closed.is_set() or instance.phase == "stopped"
                    or self.instances.get(instance.key) is not instance):
                return
            if idle_seconds is not None and (
                    instance.spec.mode != "lazy" or instance.phase != "ready" or instance.in_flight
                    or (expected_activity is not None and instance.last_activity != expected_activity)
                    or self.clock() - instance.last_activity < idle_seconds):
                return
            if only_unreferenced and instance.refs:
                return
            if require_idle and (instance.in_flight or instance.phase == "starting"):
                raise GatewayError("busy", "MCP has active calls or is starting; retry after completion")
            if not instance.stop_context:
                instance.stop_context = {"reason_code": reason or (
                    "idle_timeout" if idle_seconds is not None else
                    "unreferenced" if only_unreferenced else "stop_requested")}
                if lease_id:
                    instance.stop_context["lease_id"] = lease_id
                if operation_id:
                    instance.stop_context["operation_id"] = operation_id
                if idle_seconds is not None:
                    instance.stop_context.update(idle_seconds=max(0., self.clock() - instance.last_activity),
                                                 idle_limit_seconds=idle_seconds)
            if clear_refs:
                instance.refs.clear()
            instance.force_stop |= force
            task = instance.stop_task
            if task is None or task.done():
                instance.phase = "draining"
                task = instance.stop_task = asyncio.create_task(self._finish_stop(instance))
        # Callers may be cancelled independently; lifecycle cleanup has one owner.
        await asyncio.shield(task)

    async def _finish_stop(self, instance: Instance):
        timeout = float(instance.spec.config.get("stop_timeout", 10))
        try:
            await asyncio.wait_for(instance.drained.wait(), timeout)
        except TimeoutError:
            if not instance.force_stop:
                return
        lock = self._locks.setdefault(instance.key, asyncio.Lock())
        async with lock:
            if instance.closed.is_set():
                return
            instance.phase = "stopping"
            if not instance.stop.is_set():
                instance.stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(instance.closed.wait()), timeout)
        except TimeoutError:
            if instance.owner:
                instance.owner.cancel()
                await asyncio.gather(instance.owner, return_exceptions=True)

    async def release(self, lease_id: str, *, keep_alive=False, if_expired=False, reason="lease_released"):
        lease = self.leases.get(lease_id)
        if if_expired and (not lease or not self.lease_expired(lease)):
            return False
        self.leases.pop(lease_id, None)
        affected = [instance for instance in self.instances.values() if lease_id in instance.refs]
        operation_id = None
        if if_expired and lease:
            operation_id = uuid.uuid4().hex
            interval, missed = self.bridge_heartbeat_policy(lease)
            heartbeat_lost = interval > 0 and self.clock() - lease.heartbeat_tick >= interval * missed
            # The lease has already been revoked. Instance closures are separate facts.
            await self._emit_lifecycle("lease.disconnect", {
                "lease_id": lease_id, "owner_id": lease.user_id, "token_id": lease.token_id,
                "kind": lease.kind, "operation_id": operation_id,
                "reason_code": "bridge_heartbeat_timeout" if heartbeat_lost else "lease_timeout",
                "idle_seconds": max(0., self.clock() - lease.touched),
                "timeout_seconds": self.lease_timeout(lease),
                "heartbeat_timeout_seconds": interval * missed if interval else None,
                "mcp_ids": sorted({instance.spec.id for instance in affected}),
                "instance_ids": [instance.generation for instance in affected], "released": True,
            }, target_type="lease", target_id=lease_id)
        for instance in affected:
            lock = self._locks.setdefault(instance.key, asyncio.Lock())
            async with lock:
                instance.refs.discard(lease_id)
                stop = not keep_alive and not instance.refs and instance.spec.mode == "lazy"
            if stop:
                await self._stop_instance(instance, only_unreferenced=True,
                                          reason="lease_expired" if if_expired else reason,
                                          lease_id=lease_id, operation_id=operation_id)
        return lease is not None

    async def stop_server(self, server_id: str, *, force=False, require_idle=False, revision=None, reason=None):
        instances = [i for i in self.instances.values() if i.spec.id == server_id
                     and (revision is None or i.spec.revision == revision)]
        if require_idle and any(i.in_flight or i.phase == "starting" for i in instances):
            raise GatewayError("busy", "MCP has active calls or is starting; retry after completion")
        for instance in instances:
            await self._stop_instance(instance, force=force, require_idle=require_idle, clear_refs=True, reason=reason)

    async def invalidate_credential(self, server_id, owner):
        self.credential_versions[(server_id, owner)] = None
        for instance in list(self.instances.values()):
            if instance.spec.id == server_id and instance.spec.credential_owner == owner:
                await self._stop_instance(instance, clear_refs=True, reason="credential_changed")

    async def restrict_lease(self, lease_id, allowed):
        for instance in list(self.instances.values()):
            if lease_id in instance.refs and instance.spec.id not in allowed:
                instance.refs.discard(lease_id)
                if not instance.refs and instance.spec.mode == "lazy":
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
        owned.extend(i for i in self.instances.values() if i not in owned
                     and i.spec.isolation == "session"
                     and (user_id is None or i.admitted_user == user_id)
                     and (token_id is None or i.admitted_token == token_id))
        for lease in list(self.leases.values()):
            if (user_id is None or lease.user_id == user_id) and (
                    token_id is None or lease.token_id == token_id):
                await self.release(lease.id)
        for instance in owned:
            await self._stop_instance(instance, clear_refs=True)

    async def reap(self):
        for lease in list(self.leases.values()):
            if self.lease_expired(lease):
                await self.release(lease.id, if_expired=True)
        for instance in list(self.instances.values()):
            idle = float(instance.spec.config.get("idle_seconds", self.idle_seconds))
            if instance.phase == "draining" and instance.in_flight == 0:
                await self._stop_instance(instance)
            elif instance.spec.mode == "lazy" and idle > 0:
                await self._stop_instance(instance, idle_seconds=idle,
                                          expected_activity=instance.last_activity, clear_refs=True)

    def lease_status(self, *, user_id: str | None = None):
        """Copy runtime metadata without I/O, credentials or downstream startup."""
        now = self.clock()
        bindings = {}
        for instance in self.instances.values():
            snapshot = {
                "server_id": instance.spec.id, "generation": instance.generation,
                "phase": instance.phase, "mode": instance.spec.mode,
                "isolation": instance.spec.isolation, "lease_count": len(instance.refs),
                "in_flight": instance.in_flight, "waiters": instance.waiters,
            }
            for lease_id in instance.refs:
                bindings.setdefault(lease_id, []).append(snapshot.copy())
        result = []
        for lease in self.leases.values():
            if user_id is not None and lease.user_id != user_id:
                continue
            idle = max(0., now - lease.touched)
            ttl = self.lease_timeout(lease)
            touched_at = lease.created + (lease.touched - lease.created_clock)
            remaining = None if ttl is None else max(0., ttl - idle)
            expired = self.lease_expired(lease, now=now)
            interval, missed_heartbeats = self.bridge_heartbeat_policy(lease)
            status = ("expired" if expired else "active" if lease.in_flight or lease.pending else
                      "expiring" if remaining is not None and remaining <= 15 else "idle")
            result.append({
                "id": lease.id, "lease_id": lease.id,
                "user_id": lease.user_id, "token_id": lease.token_id, "kind": lease.kind,
                "created_at": lease.created, "touched_at": touched_at, "idle_seconds": idle,
                "ttl": ttl, "expires_in": remaining,
                "expires_at": None if ttl is None else touched_at + ttl,
                "expired": expired, "status": status, "in_flight": lease.in_flight,
                "queued_calls": max(0, lease.pending - lease.in_flight),
                "call_count": lease.call_count,
                "last_call_at": (None if lease.last_call_tick is None else
                                 lease.created + lease.last_call_tick - lease.created_clock),
                "bridge_heartbeat_seconds": lease.heartbeat_seconds if lease.kind == "bridge" else None,
                "bridge_heartbeat_detection_enabled": interval > 0 if lease.kind == "bridge" else None,
                "bridge_missed_heartbeats": (min(missed_heartbeats,
                    int(max(0, now - lease.heartbeat_tick) // interval))
                    if interval else None),
                "bridge_disconnect_at": (lease.created + lease.heartbeat_tick - lease.created_clock
                    + interval * missed_heartbeats
                    if interval else None),
                "instances": bindings.get(lease.id, []),
            })
        return result

    def capacity_status(self):
        active_instances = [
            item for item in self.instances.values() if not item.closed.is_set()
        ]
        status = self._business_gate.status()
        status.update(
            instances=len(active_instances),
            instances_by_user=dict(Counter(
                item.admitted_user for item in active_instances
            )),
            instance_waiters=sum(item.waiters for item in active_instances),
        )
        return status

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
        await asyncio.gather(*(self._stop_instance(i, force=True, reason="shutdown")
                               for i in list(self.instances.values())))


def oauth_http_401(error):
    """Find an explicit HTTP 401 through SDK task-group/cause wrappers."""
    pending, visited = [error], set()
    while pending:
        item = pending.pop()
        if id(item) in visited:
            continue
        visited.add(id(item))
        if (getattr(item, "oauth_http_401", False)
                or getattr(getattr(item, "response", None), "status_code", None) == 401):
            return True
        pending.extend(getattr(item, "exceptions", ()))
        pending.extend(value for value in (getattr(item, "__cause__", None), getattr(item, "__context__", None)) if value)
    return False
