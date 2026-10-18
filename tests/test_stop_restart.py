"""Console stop ends current instances while preserving enabled service policies."""
import asyncio
from contextlib import asynccontextmanager

import pytest
from test_bug1_catalog import Connection, console

from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("mode", ["lazy", "eager"])
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_stop_allows_next_business_call_to_restart(tmp_path, mode, transport):
    async with console(tmp_path) as (app, web, _actor):
        starts = []
        catalog, runtime = app.state.catalog, app.state.runtime

        @asynccontextmanager
        async def connect(spec):
            starts.append(spec.id)
            yield Connection()

        runtime.connector = connect
        config = {"command": "fake"} if transport == "stdio" else {"url": "https://mcp.test"}
        row = await catalog.create({
            "name": "restart", "mode": mode, "transport": transport, "config": config,
        })
        lease = runtime.create_lease("system", "agent")
        try:
            await catalog.call(row, lease, "first", {"value": "initial"})
            before = starts.count(row.id)
            for _ in range(2):
                response = await web.post(f"/api/v1/mcps/{row.id}/stop")
                assert response.status_code == 200, response.text
                assert runtime.status() == []
            assert starts.count(row.id) == before
            current = await catalog.get(row.id)
            assert current.mode == mode
            assert current.revision == row.revision
            result = await catalog.call(current, lease, "first", {"value": "restarted"})
            assert result["content"][0]["text"] == "ok"
            assert starts.count(row.id) == before + 1
            assert runtime.status()[0]["phase"] == "ready"
            await runtime.release(lease.id)
            assert bool(runtime.status()) == (mode == "eager")
        finally:
            await runtime.release(lease.id)


@pytest.mark.parametrize("mode", ["lazy", "eager"])
async def test_stop_forces_active_connection_closed_and_next_call_succeeds(tmp_path, mode):
    async with console(tmp_path) as (app, web, _actor):
        runtime, catalog = app.state.runtime, app.state.catalog
        entered, closed = asyncio.Event(), asyncio.Event()
        block = [True]
        calls = []

        class Peer(Connection):
            async def call(self, name, arguments):
                calls.append(arguments["value"])
                if block[0]:
                    entered.set()
                    await closed.wait()
                    raise ConnectionError("connection forcibly stopped")
                return await super().call(name, arguments)

        @asynccontextmanager
        async def connect(spec):
            try:
                yield Peer()
            finally:
                closed.set()

        runtime.connector = connect
        row = await catalog.create({
            "name": "busy", "mode": mode, "config": {"command": "fake", "stop_timeout": .02},
        })
        # Initial lazy discovery closes its connection before the business call.
        closed.clear()
        lease = runtime.create_lease("system", "agent")
        active = asyncio.create_task(catalog.call(row, lease, "first", {"value": "active"}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            response = await asyncio.wait_for(web.post(f"/api/v1/mcps/{row.id}/stop"), 2)
            assert response.status_code == 200, response.text
            assert closed.is_set()
            with pytest.raises(GatewayError) as interrupted:
                await asyncio.wait_for(active, 2)
            assert interrupted.value.code == "outcome_unknown"
            assert runtime.status() == []
            block[0] = False
            result = await catalog.call(row, lease, "first", {"value": "new"})
            assert result["content"][0]["text"] == "ok"
            assert calls == ["active", "new"]
        finally:
            closed.set()
            await asyncio.gather(active, return_exceptions=True)
            await runtime.release(lease.id)
