import asyncio
from contextlib import asynccontextmanager

import pytest

from mcp_manager import bridge


async def test_heartbeat_survives_transient_failure_and_requests_reconnect(monkeypatch):
    class LeaseHTTP:
        attempts = 0
        async def post(self, path):
            self.attempts += 1
            if self.attempts == 1:
                raise bridge.httpx.ConnectError("temporary outage")
            return bridge.httpx.Response(200, request=bridge.httpx.Request("POST", "http://test" + path))
    upstream = bridge.BridgeUpstream("http://test", "")
    http = LeaseHTTP()
    task = asyncio.create_task(upstream.heartbeat(http, "lease", .001))
    try:
        for _ in range(100):
            if http.attempts >= 2:
                break
            await asyncio.sleep(.002)
        assert http.attempts >= 2
        assert not task.done()
        assert upstream.stale
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_failed_business_is_not_replayed_and_next_request_reconnects(monkeypatch):
    events = []
    @asynccontextmanager
    async def connection(self):
        generation = len([e for e in events if e == "connect"]) + 1
        events.append("connect")
        class Client:
            async def call_tool(self, name, arguments):
                events.append(("call", generation, name))
                if name == "uncertain":
                    raise RuntimeError("response lost after dispatch")
                return generation
        yield Client()
        events.append("close")
    monkeypatch.setattr(bridge.BridgeUpstream, "connection", connection)
    async with bridge.BridgeUpstream("http://test", "") as upstream:
        with pytest.raises(RuntimeError, match="response lost"):
            await upstream.request("call_tool", "uncertain", {})
        assert events.count(("call", 1, "uncertain")) == 1
        assert await upstream.request("call_tool", "next", {}) == 2
    assert events == ["connect", ("call", 1, "uncertain"), "close", "connect", ("call", 2, "next"), "close"]


async def test_bridge_keeps_concurrent_requests_and_fails_fast_when_owner_stops(monkeypatch):
    entered, unblock = asyncio.Event(), asyncio.Event()
    calls = 0
    @asynccontextmanager
    async def connection(self):
        class Client:
            async def call_tool(self, name, arguments):
                nonlocal calls
                calls += 1
                if calls == 2:
                    entered.set()
                await unblock.wait()
                return name
        yield Client()
    monkeypatch.setattr(bridge.BridgeUpstream, "connection", connection)
    async with bridge.BridgeUpstream("http://test", "") as upstream:
        first = asyncio.create_task(upstream.request("call_tool", "first", {}))
        second = asyncio.create_task(upstream.request("call_tool", "second", {}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
        finally:
            unblock.set()
        assert await asyncio.gather(first, second) == ["first", "second"]
        upstream.task.cancel()
        await asyncio.gather(upstream.task, return_exceptions=True)
        with pytest.raises(RuntimeError, match="closed"):
            await asyncio.wait_for(upstream.request("call_tool", "third", {}), 1)
