"""Capacity gates for large catalogs and concurrent calls across many MCPs."""

import asyncio
import os
import statistics
import time
from contextlib import asynccontextmanager

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.catalog_store import CatalogStore
from mcp_manager.catalog_types import CatalogScope
from mcp_manager.database import McpServer
from mcp_manager.runtime import Runtime, RuntimeCapacity, ServerSpec


async def test_ten_thousand_tool_catalog_has_bounded_warm_queries(dual_db, db_settings):
    count = int(os.environ.get("MMG_CAPACITY_TOOL_COUNT", "10000"))
    async with dual_db.locked() as session:
        row = McpServer(name="Capacity", slug="capacity", transport="stdio", config={})
        session.add(row)
        await session.flush()
    store = CatalogStore(dual_db, ArtifactStore(db_settings.data_dir), signing_key=db_settings.jwt_secret)
    scope = CatalogScope(row.id, "service", row.revision, "shared")
    generation = await store.begin_refresh(scope)
    for start in range(0, count, 250):
        await store.stage_page(generation.id, "tools", [
            {
                "name": f"tool-{index:05d}",
                "description": "weather forecast common" if index % 100 == 0 else "ordinary utility",
                "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}},
            }
            for index in range(start, min(count, start + 250))
        ])
    await store.publish(generation.id, expected_scope=scope)
    await store.query_tool_entries([generation.id], query="weather forecast", limit=50)
    samples = []
    for _ in range(20):
        started = time.perf_counter()
        total, rows = await store.query_tool_entries([generation.id], query="weather forecast", limit=50)
        samples.append((time.perf_counter() - started) * 1000)
        assert total == min(100, (count + 99) // 100)
        assert len(rows) <= 50
    p95 = statistics.quantiles(samples, n=20)[18]
    assert p95 <= 200, {"warm_query_p95_ms": round(p95, 2), "samples": samples}


class Endpoint:
    async def call(self, _name, arguments):
        started = time.perf_counter()
        await asyncio.sleep(0.008 if arguments["slow"] else 0.001)
        return {
            "content": [{"type": "text", "text": arguments["server"]}],
            "service_ms": (time.perf_counter() - started) * 1000,
        }


@asynccontextmanager
async def connect(_spec):
    yield Endpoint()


def rss_bytes():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.WorkingSetSize
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


async def _concurrent_mcp_sample(server_count):
    capacity = RuntimeCapacity(
        global_calls=32,
        user_calls=32,
        mcp_calls=4,
        queued_calls=max(256, server_count),
        global_instances=server_count,
        user_instances=server_count,
        per_instance_waiters=4,
        queue_timeout=10,
    )
    runtime = Runtime(connect, capacity=capacity)
    lease = runtime.create_lease("load-user", "load-token")
    latencies = []
    service_latencies = []
    loop_lag = []
    stop = asyncio.Event()

    async def heartbeat():
        deadline = asyncio.get_running_loop().time() + 0.005
        while not stop.is_set():
            await asyncio.sleep(0.005)
            now = asyncio.get_running_loop().time()
            loop_lag.append(max(0, now - deadline) * 1000)
            deadline = now + 0.005

    async def invoke(index):
        started = time.perf_counter()
        server = f"mcp-{index:04d}"
        result = await runtime.call(
            ServerSpec(id=server, transport="stdio", config={"concurrency": 4}, mode="lazy"),
            lease.id,
            "echo",
            {"server": server, "slow": index % 10 == 0},
        )
        latencies.append((time.perf_counter() - started) * 1000)
        service_latencies.append(result["service_ms"])
        assert result["content"][0]["text"] == server

    rss_before = rss_bytes()
    ticker = asyncio.create_task(heartbeat())
    started = time.perf_counter()
    await asyncio.gather(*(invoke(index) for index in range(server_count)))
    elapsed = time.perf_counter() - started
    stop.set()
    await ticker
    rss_after = rss_bytes()
    rss_growth = max(0, rss_after - rss_before)
    capacity_status = runtime.capacity_status()
    await runtime.close()
    ordered = sorted(latencies)
    services = sorted(service_latencies)
    return {
        "mcp_count": server_count,
        "throughput_per_second": server_count / elapsed,
        "p50_ms": statistics.median(ordered),
        "p95_ms": ordered[max(0, int(len(ordered) * 0.95) - 1)],
        "p99_ms": ordered[max(0, int(len(ordered) * 0.99) - 1)],
        "service_p95_ms": services[max(0, int(len(services) * 0.95) - 1)],
        "queue_delay_ms_max": capacity_status["queue_delay_ms_max"],
        "loop_lag_p99_ms": sorted(loop_lag)[max(0, int(len(loop_lag) * 0.99) - 1)] if loop_lag else 0,
        "rss_bytes": rss_after,
        "rss_growth_bytes": rss_growth,
        "rejection_rate": 0,
    }


async def test_concurrent_calls_across_100_and_1000_mcps_are_fast_and_bounded():
    results = [await _concurrent_mcp_sample(count) for count in (100, 1000)]
    for result in results:
        print(result)
        assert result["service_p95_ms"] <= 100, result
        assert result["p95_ms"] <= 10_000, result
        assert result["loop_lag_p99_ms"] <= 100, result
        assert 0 < result["rss_bytes"] <= 1024 * 1024 * 1024, result
        assert result["rss_growth_bytes"] <= 128 * 1024 * 1024, result
        assert result["rejection_rate"] == 0, result
