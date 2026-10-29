"""Lifecycle facts are audited once, after real transitions, with causal lease IDs."""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from test_runtime_lifecycle import Connection

from mcp_manager.logs import LogStore
from mcp_manager.runtime import GatewayError, Runtime, ServerSpec


@asynccontextmanager
async def observed(tmp_path, **options):
    ticks = [0.]
    connection = Connection()
    logs = LogStore(tmp_path)
    runtime = Runtime(connection.connect, clock=lambda: ticks[0],
                      **({"idle_seconds": 10, "connection_ttl_seconds": 0} | options))
    runtime.on_lifecycle = logs.audit
    try:
        yield runtime, connection, logs, ticks
    finally:
        await runtime.close()
        await logs.close()


async def events(logs, action):
    return [await logs.audits.detail(row["event_id"])
            for row in (await logs.audits.query(action=action))["items"]]


async def test_calls_and_manual_stops_do_not_add_automatic_or_start_audits(tmp_path):
    async with observed(tmp_path) as (runtime, _connection, logs, _ticks):
        spec = ServerSpec("mcp", "stdio", {}, name="Named MCP")
        lease = runtime.create_lease("alice", "token-id")
        for _ in range(2):
            await runtime.call(spec, lease.id, "call", {})
        assert not await events(logs, "mcp.start")
        assert not await events(logs, "mcp.stop")
        await runtime.stop_server("mcp", force=True, reason="manual_stop")
        await runtime.stop_server("mcp", force=True, reason="manual_stop")
        assert not await events(logs, "mcp.stop")
        assert not await events(logs, "mcp.start")


async def test_idle_stop_preserves_lease_and_next_call_has_new_instance(tmp_path):
    async with observed(tmp_path) as (runtime, _connection, logs, ticks):
        spec = ServerSpec("mcp", "stdio", {}, name="Idle MCP")
        lease = runtime.create_lease("alice", "token-id")
        await runtime.call(spec, lease.id, "call", {})
        first_instance = next(iter(runtime.instances.values())).generation
        ticks[0] = 11
        await runtime.reap()
        assert lease.id in runtime.leases and not runtime.instances
        await runtime.reap()
        stops = await events(logs, "mcp.stop")
        assert len(stops) == 1 and stops[0]["details"]["reason_code"] == "idle_timeout"
        assert stops[0]["details"]["idle_seconds"] >= 10
        assert not await events(logs, "lease.disconnect")
        await runtime.call(spec, lease.id, "call", {})
        assert next(iter(runtime.instances.values())).generation != first_instance
        assert not await events(logs, "mcp.start")


async def test_expired_last_lease_links_its_release_to_actual_lazy_stops(tmp_path):
    async with observed(tmp_path, connection_ttl_seconds=10) as (runtime, _connection, logs, ticks):
        lease = runtime.create_lease("alice", "token-id", kind="retention")
        identities = ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"]
        for identity in identities:
            await runtime.call(ServerSpec(identity, "stdio", {}, name=identity), lease.id, "call", {})
        ticks[0] = 11
        await asyncio.gather(runtime.release(lease.id, if_expired=True),
                             runtime.release(lease.id, if_expired=True))
        await runtime.reap()
        releases = await events(logs, "lease.disconnect")
        stops = await events(logs, "mcp.stop")
        assert len(releases) == 1 and len(stops) == 2
        released = releases[0]
        assert released["target_type"] == "lease" and released["target_id"] == lease.id
        assert released["details"]["owner_id"] == "alice"
        assert set(released["details"]["mcp_ids"]) == set(identities)
        assert released["details"]["reason_code"] == "lease_timeout"
        for item in (await logs.audits.query(action="mcp.stop"))["items"]:
            assert len(item["summary"]) <= 300
            assert json.loads(item["summary"])["reason_code"] == "lease_expired"
        for stopped in stops:
            assert stopped["details"]["reason_code"] == "lease_expired"
            assert stopped["details"]["lease_id"] == lease.id
            assert stopped["details"]["operation_id"] == released["details"]["operation_id"]
            assert stopped["details"]["instance_id"] in released["details"]["instance_ids"]
            assert stopped["actor_type"] == stopped["source"] == "system"
            assert stopped["user_id"] is None and stopped["result"] == "success"


@pytest.mark.parametrize("mode", ["lazy", "eager"])
async def test_expiry_does_not_claim_shared_or_eager_instance_has_stopped(tmp_path, mode):
    async with observed(tmp_path, idle_seconds=0, connection_ttl_seconds=10) as (runtime, _connection, logs, ticks):
        spec = ServerSpec("shared", "stdio", {}, mode=mode)
        first = runtime.create_lease("alice", "a")
        second = runtime.create_lease("bob", "b")
        await runtime.call(spec, first.id, "call", {})
        await runtime.call(spec, second.id, "call", {})
        ticks[0] = 5
        runtime.heartbeat(second.id, "bob", "b")
        ticks[0] = 11
        await runtime.reap()
        assert len(await events(logs, "lease.disconnect")) == 1
        assert not await events(logs, "mcp.stop")
        assert second.id in runtime.leases and len(runtime.instances) == 1
        ticks[0] = 16
        await runtime.reap()
        assert len(await events(logs, "lease.disconnect")) == 2
        assert len(await events(logs, "mcp.stop")) == (1 if mode == "lazy" else 0)


async def test_inflight_calls_do_not_generate_false_expiry_or_idle_events(tmp_path):
    async with observed(tmp_path, connection_ttl_seconds=10) as (runtime, connection, logs, ticks):
        lease = runtime.create_lease("alice", "t")
        connection.block = True
        call = asyncio.create_task(runtime.call(ServerSpec("busy", "stdio", {}), lease.id, "call", {}))
        try:
            await asyncio.wait_for(connection.entered.wait(), 2)
            ticks[0] = 11
            await runtime.reap()
            assert not await events(logs, "lease.disconnect")
            assert not await events(logs, "mcp.stop")
        finally:
            connection.finish.set()
            await call


async def test_stop_is_recorded_after_transport_cleanup_not_at_stop_request(tmp_path):
    async with observed(tmp_path) as (runtime, _connection, logs, _ticks):
        closing, allow_close = asyncio.Event(), asyncio.Event()
        @asynccontextmanager
        async def connect(_spec):
            try:
                yield Connection()
            finally:
                closing.set()
                await allow_close.wait()
        runtime.connector = connect
        lease = runtime.create_lease("alice", "t")
        await runtime.call(ServerSpec("slow-close", "stdio", {}), lease.id, "call", {})
        stopping = asyncio.create_task(runtime.stop_server("slow-close", reason="configuration_changed"))
        try:
            await asyncio.wait_for(closing.wait(), 2)
            assert not await events(logs, "mcp.stop")
        finally:
            allow_close.set()
            await stopping
        assert len(await events(logs, "mcp.stop")) == 1


async def test_failed_start_does_not_fabricate_successful_start_or_stop(tmp_path):
    async with observed(tmp_path) as (runtime, _connection, logs, _ticks):
        @asynccontextmanager
        async def fail(_spec):
            raise GatewayError("startup_failed", "secret-fixture-value")
            yield  # pragma: no cover
        runtime.connector = fail
        lease = runtime.create_lease("alice", "t")
        with pytest.raises(GatewayError):
            await runtime.call(ServerSpec("bad", "stdio", {"password": "secret-fixture-value"}), lease.id, "call", {})
        assert not await events(logs, "mcp.start")
        assert not await events(logs, "mcp.stop")


async def test_bridge_heartbeat_expiry_and_shutdown_have_distinct_reasons(tmp_path):
    async with observed(tmp_path, bridge_heartbeat_seconds=2, bridge_missed_heartbeats=3) as (
        runtime, _connection, logs, ticks,
    ):
        bridge = runtime.create_lease("alice", "t", kind="bridge")
        await runtime.call(ServerSpec("eager", "stdio", {}, mode="eager"), bridge.id, "call", {})
        ticks[0] = 7
        await runtime.reap()
        expired = await events(logs, "lease.disconnect")
        assert len(expired) == 1 and expired[0]["details"]["reason_code"] == "bridge_heartbeat_timeout"
        assert not await events(logs, "mcp.stop")
        await runtime.close()
        assert (await events(logs, "mcp.stop"))[0]["details"]["reason_code"] == "shutdown"


async def test_audit_callback_failure_never_prevents_start_or_cleanup(tmp_path):
    async with observed(tmp_path) as (runtime, connection, _logs, _ticks):
        async def fail(*_args, **_kwargs):
            raise OSError("simulated audit disk failure")
        runtime.on_lifecycle = fail
        lease = runtime.create_lease("alice", "t")
        await runtime.call(ServerSpec("mcp", "stdio", {}), lease.id, "call", {})
        await runtime.release(lease.id)
        assert not runtime.instances and connection.closed == 1
