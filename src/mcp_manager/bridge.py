"""A lightweight stdio client bridge; all children remain owned by the gateway."""
import asyncio
import contextlib

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .about import NAME, VERSION
from .protocol_errors import is_request_error


class BridgeUpstream:
    """Own SDK contexts in one task and reconnect only before a new request."""

    def __init__(self, base, token):
        self.base, self.token = base, token
        self.queue = asyncio.Queue()
        self.stale = False

    async def __aenter__(self):
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.run())
        try:
            await self.ready
        except BaseException:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            self.task.cancel()
        else:
            await self.queue.put(None)
        await asyncio.gather(self.task, return_exceptions=True)

    async def heartbeat(self, http, lease_id, seconds):
        while True:
            await asyncio.sleep(seconds)
            try:
                response = await http.post("/gateway/v1/leases/" + lease_id + "/heartbeat")
                response.raise_for_status()
            except httpx.HTTPError:
                # Keep retrying heartbeats; the next business request gets a fresh
                # lease and MCP connection. Never replay a request already sent.
                self.stale = True

    @contextlib.asynccontextmanager
    async def connection(self):
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        async with httpx.AsyncClient(base_url=self.base, headers=headers, timeout=30, trust_env=False) as lease_http:
            response = await lease_http.post("/gateway/v1/leases")
            response.raise_for_status()
            lease = response.json()
            lease_id = lease["id"]
            headers["X-MCP-Manager-Lease"] = lease_id
            if lease.get("client_secret"):
                headers["X-MCP-Manager-Client"] = lease["client_secret"]
                lease_http.headers["X-MCP-Manager-Client"] = lease["client_secret"]
            task = asyncio.create_task(self.heartbeat(lease_http, lease_id, lease.get("heartbeat_seconds", 30)))
            try:
                async with httpx2.AsyncClient(headers=headers, timeout=300, trust_env=False) as http:
                    async with Client(streamable_http_client(self.base + "/mcp", http_client=http),
                                      cache=None, read_timeout_seconds=300) as client:
                        yield client
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                with contextlib.suppress(httpx.HTTPError):
                    await lease_http.delete("/gateway/v1/leases/" + lease_id)

    async def request(self, method, *args, **kwargs):
        if self.task.done():
            raise RuntimeError("Bridge connection closed")
        result = asyncio.get_running_loop().create_future()
        await self.queue.put((result, method, args, kwargs))
        try:
            # The owner may die immediately after the queue check.
            await asyncio.wait({result, self.task}, return_when=asyncio.FIRST_COMPLETED)
            if result.done():
                return result.result()
            raise RuntimeError("Bridge connection closed")
        finally:
            if not result.done():
                result.cancel()

    async def dispatch(self, client, current, method, args, kwargs):
        try:
            result = await getattr(client, method)(*args, **kwargs)
        except asyncio.CancelledError:
            if not current.done():
                current.set_exception(RuntimeError("Bridge connection closed"))
            raise
        except Exception as exc:
            if not is_request_error(exc):
                self.stale = True
            if not current.done():
                current.set_exception(exc)
        else:
            if not current.done():
                current.set_result(result)

    async def run(self):
        current = None
        in_flight = set()
        try:
            async with contextlib.AsyncExitStack() as stack:
                client = await stack.enter_async_context(self.connection())
                self.ready.set_result(None)
                try:
                    while True:
                        item = await self.queue.get()
                        if item is None:
                            await asyncio.gather(*in_flight, return_exceptions=True)
                            return
                        current, method, args, kwargs = item
                        if current.cancelled():
                            continue
                        try:
                            if self.stale:
                                # Finish already dispatched calls on their original
                                # connection. Only the new request uses the replacement.
                                await asyncio.gather(*in_flight, return_exceptions=True)
                                await stack.aclose()
                                self.stale = False
                                client = await stack.enter_async_context(self.connection())
                            if not current.cancelled():
                                task = asyncio.create_task(self.dispatch(client, current, method, args, kwargs))
                                in_flight.add(task)
                                task.add_done_callback(in_flight.discard)
                        except Exception as exc:
                            self.stale = True
                            if not current.done():
                                current.set_exception(exc)
                        finally:
                            current = None
                finally:
                    for task in in_flight:
                        task.cancel()
                    await asyncio.gather(*in_flight, return_exceptions=True)
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            if current and not current.done():
                current.set_exception(RuntimeError("Bridge connection closed"))
            raise
        finally:
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if item and not item[0].done():
                    item[0].set_exception(RuntimeError("Bridge connection closed"))


def create_bridge_server(upstream):
    async def tools(ctx, params):
        return await upstream.request("list_tools", cursor=params.cursor if params else None, cache_mode="bypass")

    async def call(ctx, params):
        return await upstream.request("call_tool", params.name, params.arguments or {})

    async def resources(ctx, params):
        return await upstream.request("list_resources", cursor=params.cursor if params else None, cache_mode="bypass")

    async def templates(ctx, params):
        return await upstream.request("list_resource_templates", cursor=params.cursor if params else None,
                                      cache_mode="bypass")

    async def read(ctx, params):
        return await upstream.request("read_resource", params.uri, cache_mode="bypass")

    async def prompts(ctx, params):
        return await upstream.request("list_prompts", cursor=params.cursor if params else None, cache_mode="bypass")

    async def prompt(ctx, params):
        return await upstream.request("get_prompt", params.name, params.arguments or {})

    return Server(NAME, version=VERSION, on_list_tools=tools, on_call_tool=call,
                  on_list_resources=resources, on_list_resource_templates=templates,
                  on_read_resource=read, on_list_prompts=prompts, on_get_prompt=prompt)


async def run_bridge(url, token):
    base = url.rstrip("/")
    if base.endswith("/mcp"):
        base = base[:-4]
    async with BridgeUpstream(base, token) as upstream:
        server = create_bridge_server(upstream)
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
