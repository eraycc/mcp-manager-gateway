"""Request errors must not destroy healthy shared MCP connections."""
import asyncio
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CONNECTION_CLOSED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    REQUEST_TIMEOUT,
)

from mcp_manager.bridge import BridgeUpstream
from mcp_manager.runtime import GatewayError, Runtime, ServerSpec
from mcp_manager.transports import connect

REQUEST_CODES = [INVALID_PARAMS, METHOD_NOT_FOUND, -32010, -32042, 1001]
RECOVERY_CODES = [CONNECTION_CLOSED, REQUEST_TIMEOUT, INVALID_REQUEST, INTERNAL_ERROR, PARSE_ERROR]


class Peer:
    def __init__(self, error):
        self.error = error
        self.starts = 0
        self.closes = 0
        self.calls = []
        self.entered = asyncio.Event()
        self.finish = asyncio.Event()

    @asynccontextmanager
    async def connect(self, *args):
        self.starts += 1
        try:
            yield self
        finally:
            self.closes += 1

    async def call(self, name, arguments):
        self.calls.append(name)
        if name == "reject":
            raise self.error
        if name == "slow":
            self.entered.set()
            await self.finish.wait()
        return {"content": [], "isError": name == "tool_error"}

    call_tool = call


@pytest.mark.parametrize("code", REQUEST_CODES)
async def test_runtime_request_error_preserves_shared_instance_and_leases(code):
    error = MCPError(code, "request rejected")
    peer = Peer(error)
    runtime = Runtime(peer.connect)
    spec = ServerSpec("shared", "stdio", {})
    a = runtime.create_lease("alice", "token-a")
    b = runtime.create_lease("bob", "token-b")
    try:
        await runtime.call(spec, a.id, "ok", {})
        await runtime.call(spec, b.id, "ok", {})
        instance = next(iter(runtime.instances.values()))
        with pytest.raises(GatewayError) as caught:
            await runtime.call(spec, a.id, "reject", {})
        assert caught.value.code == "downstream_error"
        assert caught.value.__cause__ is error
        assert str(caught.value) == "request rejected"
        assert instance.phase == "ready"
        assert instance.refs == {a.id, b.id}
        assert next(iter(runtime.instances.values())) is instance
        assert await runtime.call(spec, b.id, "ok", {}) == {"content": [], "isError": False}
        assert peer.starts == 1 and peer.closes == 0
        assert peer.calls.count("reject") == 1
    finally:
        await runtime.close()


@pytest.mark.parametrize("code", REQUEST_CODES)
async def test_bridge_request_error_keeps_connection_without_replay(monkeypatch, code):
    error = MCPError(code, "request rejected")
    peer = Peer(error)
    monkeypatch.setattr(BridgeUpstream, "connection", lambda self: peer.connect())
    async with BridgeUpstream("http://test", "") as upstream:
        with pytest.raises(MCPError) as caught:
            await upstream.request("call_tool", "reject", {})
        assert caught.value is error
        assert not upstream.stale
        assert await upstream.request("call_tool", "ok", {}) == {"content": [], "isError": False}
        assert peer.starts == 1 and peer.closes == 0
        assert peer.calls == ["reject", "ok"]


@pytest.mark.parametrize("code", RECOVERY_CODES)
async def test_runtime_uncertain_error_still_drains_and_next_call_reconnects(code):
    peer = Peer(MCPError(code, "connection uncertain"))
    runtime = Runtime(peer.connect)
    lease = runtime.create_lease("alice", "token")
    spec = ServerSpec("shared", "stdio", {})
    try:
        with pytest.raises(GatewayError) as caught:
            await runtime.call(spec, lease.id, "reject", {})
        assert caught.value.code == "outcome_unknown"
        assert not runtime.instances
        assert peer.closes == 1
        await runtime.call(spec, lease.id, "ok", {})
        assert peer.starts == 2
        assert peer.calls == ["reject", "ok"]
    finally:
        await runtime.close()


@pytest.mark.parametrize("code", RECOVERY_CODES)
async def test_bridge_uncertain_error_still_reconnects_next_request(monkeypatch, code):
    peer = Peer(MCPError(code, "connection uncertain"))
    monkeypatch.setattr(BridgeUpstream, "connection", lambda self: peer.connect())
    async with BridgeUpstream("http://test", "") as upstream:
        with pytest.raises(MCPError):
            await upstream.request("call_tool", "reject", {})
        assert upstream.stale
        await upstream.request("call_tool", "ok", {})
        assert peer.starts == 2 and peer.closes == 1
        assert peer.calls == ["reject", "ok"]


async def test_tool_error_result_preserves_runtime_and_bridge(monkeypatch):
    peer = Peer(None)
    runtime = Runtime(peer.connect)
    lease = runtime.create_lease("alice", "token")
    try:
        assert (await runtime.call(ServerSpec("s", "stdio", {}), lease.id, "tool_error", {}))["isError"]
        assert runtime.status()[0]["phase"] == "ready"
        assert peer.closes == 0
    finally:
        await runtime.close()
    peer = Peer(None)
    monkeypatch.setattr(BridgeUpstream, "connection", lambda self: peer.connect())
    async with BridgeUpstream("http://test", "") as upstream:
        assert (await upstream.request("call_tool", "tool_error", {}))["isError"]
        assert not upstream.stale
        assert peer.closes == 0


@pytest.mark.parametrize("code", [INVALID_PARAMS, CONNECTION_CLOSED])
async def test_bridge_recovery_waits_for_dispatched_call_without_cancelling(monkeypatch, code):
    peer = Peer(MCPError(code, "rejected"))
    monkeypatch.setattr(BridgeUpstream, "connection", lambda self: peer.connect())
    async with BridgeUpstream("http://test", "") as upstream:
        slow = asyncio.create_task(upstream.request("call_tool", "slow", {}))
        try:
            await asyncio.wait_for(peer.entered.wait(), 1)
            with pytest.raises(MCPError):
                await upstream.request("call_tool", "reject", {})
            next_call = asyncio.create_task(upstream.request("call_tool", "ok", {}))
            for _ in range(5):
                await asyncio.sleep(0)
            assert not slow.done()
            assert peer.closes == 0
            if code == CONNECTION_CLOSED:
                assert not next_call.done()
            peer.finish.set()
            assert (await asyncio.wait_for(slow, 1))["isError"] is False
            await asyncio.wait_for(next_call, 1)
            assert peer.starts == (2 if code == CONNECTION_CLOSED else 1)
            assert peer.calls.count("reject") == 1
        finally:
            peer.finish.set()
            await asyncio.gather(slow, return_exceptions=True)


@pytest.mark.parametrize("code", [INVALID_PARAMS, CONNECTION_CLOSED])
async def test_runtime_error_preserves_other_dispatched_call_until_completion(code):
    peer = Peer(MCPError(code, "rejected"))
    runtime = Runtime(peer.connect)
    a = runtime.create_lease("alice", "token-a")
    b = runtime.create_lease("bob", "token-b")
    spec = ServerSpec("shared", "stdio", {"concurrency": 2})
    slow = asyncio.create_task(runtime.call(spec, b.id, "slow", {}))
    try:
        await asyncio.wait_for(peer.entered.wait(), 1)
        with pytest.raises(GatewayError) as caught:
            await runtime.call(spec, a.id, "reject", {})
        assert caught.value.code == ("outcome_unknown" if code == CONNECTION_CLOSED else "downstream_error")
        assert not slow.done()
        assert peer.closes == 0
        peer.finish.set()
        assert (await asyncio.wait_for(slow, 1))["isError"] is False
        await runtime.call(spec, b.id, "ok", {})
        assert peer.starts == (2 if code == CONNECTION_CLOSED else 1)
        assert peer.calls.count("reject") == 1
    finally:
        peer.finish.set()
        await asyncio.gather(slow, return_exceptions=True)
        await runtime.close()


async def test_real_http_request_rejection_keeps_runtime_generation():
    calls = []

    async def tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name="echo", inputSchema={"type": "object"})])

    async def call(ctx, params):
        calls.append(params.arguments)
        if params.arguments.get("reject"):
            raise MCPError(INVALID_PARAMS, "rejected over HTTP")
        return types.CallToolResult(content=[types.TextContent(type="text", text="next call succeeds")])

    protocol = Server("request-error-fixture", on_list_tools=tools, on_call_tool=call)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = "http://127.0.0.1:" + str(sock.getsockname()[1]) + "/mcp"
    server = uvicorn.Server(uvicorn.Config(protocol.streamable_http_app(), log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    runtime = Runtime(connect)
    lease = runtime.create_lease("alice", "token")
    spec = ServerSpec("remote", "streamable-http", {"url": url, "call_timeout": 5, "startup_timeout": 5})
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(.02)
        await runtime.discover(spec, lease.id)
        instance = next(iter(runtime.instances.values()))
        generation = instance.generation
        with pytest.raises(GatewayError) as caught:
            await runtime.call(spec, lease.id, "echo", {"reject": True})
        assert caught.value.code == "downstream_error"
        assert isinstance(caught.value.__cause__, MCPError)
        assert caught.value.__cause__.code == INVALID_PARAMS
        assert str(caught.value) == "rejected over HTTP"
        result = await runtime.call(spec, lease.id, "echo", {})
        assert result["content"][0]["text"] == "next call succeeds"
        assert next(iter(runtime.instances.values())).generation == generation
        assert instance.phase == "ready" and instance.refs == {lease.id}
        assert calls == [{"reject": True}, {}]
    finally:
        await runtime.close()
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()
