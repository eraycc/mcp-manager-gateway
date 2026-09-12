"""A responsive REST peer for tests of catalog and authentication behavior."""
from contextlib import asynccontextmanager

import httpx

from mcp_manager.transports import RestConnection


@asynccontextmanager
async def rest_connect(spec):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        yield RestConnection(spec.config, client)
