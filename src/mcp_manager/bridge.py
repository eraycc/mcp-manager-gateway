"""A lightweight stdio client bridge; all children remain owned by the gateway."""
import asyncio
import contextlib

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def run_bridge(url, token):
    base = url.rstrip("/")
    if base.endswith("/mcp"):
        base = base[:-4]
    headers = {"Authorization": "Bearer " + token} if token else {}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30, trust_env=False) as lease_http:
        response = await lease_http.post("/gateway/v1/leases")
        response.raise_for_status()
        lease_id = response.json()["id"]
        headers["X-MCP-Manager-Lease"] = lease_id

        async def heartbeat():
            while True:
                await asyncio.sleep(30)
                response = await lease_http.post("/gateway/v1/leases/" + lease_id + "/heartbeat")
                response.raise_for_status()

        task = asyncio.create_task(heartbeat())
        try:
            async with httpx2.AsyncClient(headers=headers, timeout=300, trust_env=False) as http:
                async with Client(streamable_http_client(base + "/mcp", http_client=http),
                                  cache=None, read_timeout_seconds=300) as client:
                    async def tools(ctx, params):
                        return await client.list_tools(cursor=params.cursor if params else None, cache_mode="bypass")
                    async def call(ctx, params):
                        return await client.call_tool(params.name, params.arguments or {})
                    async def resources(ctx, params):
                        return await client.list_resources(cursor=params.cursor if params else None, cache_mode="bypass")
                    async def templates(ctx, params):
                        return await client.list_resource_templates(cursor=params.cursor if params else None,
                                                                    cache_mode="bypass")
                    async def read(ctx, params):
                        return await client.read_resource(params.uri, cache_mode="bypass")
                    async def prompts(ctx, params):
                        return await client.list_prompts(cursor=params.cursor if params else None, cache_mode="bypass")
                    async def prompt(ctx, params):
                        return await client.get_prompt(params.name, params.arguments or {})
                    server = Server("MCP Manager stdio bridge", on_list_tools=tools, on_call_tool=call,
                                    on_list_resources=resources, on_list_resource_templates=templates,
                                    on_read_resource=read, on_list_prompts=prompts, on_get_prompt=prompt)
                    async with stdio_server() as (reader, writer):
                        await server.run(reader, writer, server.create_initialization_options())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with contextlib.suppress(httpx.HTTPError):
                await lease_http.delete("/gateway/v1/leases/" + lease_id)
