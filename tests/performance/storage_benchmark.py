"""Reproducible bulk-fixture retrieval benchmark; no live MCP or business data."""
import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_DNS, uuid5

parser = argparse.ArgumentParser()
parser.add_argument("--source", required=True)
parser.add_argument("--data", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--label", required=True)
parser.add_argument("--services", type=int, default=10000)
parser.add_argument("--tools", type=int, default=10)
parser.add_argument("--terms", type=int, default=64)
parser.add_argument("--samples", type=int, default=30)
parser.add_argument("--query-only", action="store_true")
parser.add_argument("--store-only", action="store_true")
parser.add_argument("--pattern")
parser.add_argument("--profile", action="store_true")
parser.add_argument("--explain", action="store_true")
parser.add_argument("--purpose-flow", action="store_true")
parser.add_argument("--call-samples", type=int, default=0)
parser.add_argument("--calls-only", action="store_true")
args = parser.parse_args()
sys.path.insert(0, str(Path(args.source).resolve()))

from sqlalchemy import insert
from starlette.requests import Request

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.catalog import Catalog, alias
from mcp_manager.catalog_models import (
    ArtifactObject,
    CatalogEntry,
    CatalogGeneration,
    CatalogHead,
    CatalogTerm,
)
from mcp_manager.catalog_retrieval import index_document
from mcp_manager.catalog_store import digest, scope_key, searchable_text
from mcp_manager.catalog_types import ArtifactRef, CatalogScope
from mcp_manager.config import Settings
from mcp_manager.credentials import CredentialStore
from mcp_manager.database import Database, McpServer, now
from mcp_manager.db_types import strict_json_dumps
from mcp_manager.embeddings import EmbeddingIndex
from mcp_manager.gateway import Gateway
from mcp_manager.runtime import Runtime


def stable(value):
    return str(uuid5(NAMESPACE_DNS, "mmg-storage-benchmark:" + value))


def make_scope(identity):
    keys = sorted({f"credential:{identity}:service", f"oauth:{identity}:service"})
    fingerprint = digest(strict_json_dumps([1, [[key, None] for key in keys]]))
    return CatalogScope(identity, "service", 1, fingerprint)


def make_tool(index):
    description = "weather forecast 天气预报 common " + " ".join(f"term{part:03d}" for part in range(args.terms))
    if index % 1000 == 0:
        description += f" needle{index:06d}"
    return {"name": f"lookupWeather{index:06d}", "description": description,
            "inputSchema": {"type": "object", "properties": {
                "reference": {"type": "string", "description": "Exact reference to look up"}},
                "required": ["reference"], "additionalProperties": False}}


def rss():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak", "working", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
        value = Counters()
        value.cb = ctypes.sizeof(value)
        kernel, api = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        api.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not api.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
            raise ctypes.WinError()
        return value.working
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)


def quantiles(values):
    ordered = sorted(values)
    return {key: round(ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * probability) - 1))], 3)
            for key, probability in (("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99))}


async def seed(db, artifacts):
    servers = [{"id": stable(f"server:{index}"), "name": f"Service {index:05d}", "slug": f"service-{index:05d}",
                "transport": "stdio", "config": {}, "mode": "lazy", "isolation": "service",
                "auth_type": "none"} for index in range(args.services)]
    for start in range(0, len(servers), 250):
        async with db.locked() as session:
            await session.execute(insert(McpServer), servers[start:start + 250])
    derived = getattr(db, "derived", None)
    index = derived or db
    if derived:
        from mcp_manager.catalog_control import CatalogControl
        from mcp_manager.derived_models import CatalogServer
        for start in range(0, len(servers), 250):
            async with derived.locked() as session:
                await session.execute(insert(CatalogServer), [{
                    "id": row["id"], "slug": row["slug"], "revision": 1, "mode": "lazy", "isolation": "service"
                } for row in servers[start:start + 250]])
            async with db.locked() as session:
                await session.execute(insert(CatalogControl), [{
                    "scope_key": scope_key(scope := make_scope(row["id"])), "server_id": row["id"],
                    "owner": "service", "revision": 1, "credential_generation": scope.credential_generation,
                } for row in servers[start:start + 250]])
    postings = 0
    for start in range(0, args.services, 25):
        generation_rows, head_rows, entries, objects, terms, refs = [], [], [], [], [], []
        for server_index in range(start, min(args.services, start + 25)):
            server = servers[server_index]
            scope = make_scope(server["id"])
            generation = stable(f"generation:{server_index}")
            preview, accumulator = [], hashlib.sha256()
            for ordinal in range(args.tools):
                number = server_index * args.tools + ordinal
                value, identity = make_tool(number), stable(f"entry:{number}")
                raw = strict_json_dumps(value).encode()
                artifact = hashlib.sha256(raw).hexdigest()
                path = artifacts._path(artifact)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                refs.append(ArtifactRef(artifact, len(raw), artifact))
                name, gateway = value["name"], alias(server["slug"], value["name"])
                summary = {"name": name, "description": value["description"][:1024], "gateway_name": gateway}
                search_text = searchable_text(value)
                entries.append({"id": identity, "generation_id": generation, "kind": "tools", "original_name": name,
                    "name_hash": digest(name), "sort_key": name.encode()[:128].hex(), "gateway_name": gateway,
                    "gateway_hash": digest(gateway), "legacy_gateway_hash": digest(server["id"] + "__" + name),
                    "schema_hash": digest(strict_json_dumps(value["inputSchema"])), "summary": summary,
                    "search_text": search_text, "artifact_id": artifact, "artifact_bytes": len(raw)})
                objects.append({"id": artifact, "size_bytes": len(raw), "sha256": artifact})
                terms.extend(index_document(generation, {**value, "entry_id": identity, "search_text": search_text}))
                preview.append(name)
                accumulator.update(strict_json_dumps(["tools", digest(name), artifact]).encode())
            generation_rows.append({"id": generation, "scope_key": scope_key(scope), "server_id": server["id"],
                "owner": "service", "revision": 1, "credential_generation": scope.credential_generation,
                "status": "ready", "digest": accumulator.hexdigest(), "counts": {"tools": args.tools},
                "details": {"tools_preview": preview[:50], "generated_description": "Weather forecast lookup utilities"},
                "published_at": now()})
            head_rows.append({"scope_key": scope_key(scope), "server_id": server["id"], "owner": "service",
                "generation_id": generation, "refresh_id": generation, "status": "ready"})
        async with index.locked() as session:
            await session.execute(insert(ArtifactObject), objects)
            await session.execute(insert(CatalogGeneration), generation_rows)
            await session.execute(insert(CatalogEntry), entries)
            await session.execute(insert(CatalogHead), head_rows)
        for offset in range(0, len(terms), 5000):
            async with index.locked() as session:
                if derived:
                    from mcp_manager.catalog_terms import insert_term_rows
                    await insert_term_rows(session, terms[offset:offset + 5000])
                else:
                    await session.execute(insert(CatalogTerm), terms[offset:offset + 5000])
        if getattr(artifacts, "inventory", None):
            artifacts.inventory.register_many(refs)
        postings += len(terms)
        if start % 250 == 0:
            print(json.dumps({"phase": "fixture", "services": start + min(25, args.services - start),
                              "postings": postings}), flush=True)
    return {"services": len(servers), "entries": args.services * args.tools, "postings": postings}


async def main():
    data = Path(args.data).resolve()
    marker = data / "BENCHMARK.json"
    if not args.query_only:
        if data.exists() and any(data.iterdir()):
            raise ValueError("Benchmark refuses a nonempty fixture directory")
        data.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"label": args.label, "services": args.services, "tools": args.tools,
                                      "terms": args.terms, "complete": False}), encoding="utf-8")
    else:
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if not saved.get("complete") or (saved["services"], saved["tools"], saved["terms"]) != (
                args.services, args.tools, args.terms):
            raise ValueError("Benchmark fixture is incomplete or does not match the request")
    settings = Settings(home_dir=data / "home", data_dir=data, secret_key="benchmark-fixture-only",
                        database_url="sqlite:///" + (data / "main.sqlite").as_posix())
    db = Database(settings)
    await db.initialize()
    artifacts = ArtifactStore(data)
    runtime = Runtime(None)
    catalog, embeddings = None, None
    report = {"label": args.label, "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "fixture_kind": "bulk seeded validated model state; not production ingestion throughput",
              "cold_definition": "application caches cleared; OS cache state is unspecified", "queries": {}}
    try:
        if not args.query_only:
            started = time.perf_counter()
            report["fixture"] = await seed(db, artifacts)
            report["fixture_seconds"] = round(time.perf_counter() - started, 3)
            saved = json.loads(marker.read_text(encoding="utf-8"))
            marker.write_text(json.dumps({**saved, "complete": True, "counts": report["fixture"]}), encoding="utf-8")
        else:
            report["fixture"] = saved["counts"]
        catalog = Catalog(db, runtime, None, settings)
        catalog.credentials = CredentialStore(catalog)
        embeddings = EmbeddingIndex(data, db=db, artifacts=catalog.artifacts)
        state = SimpleNamespace(db=db, catalog=catalog, runtime=runtime, embeddings=embeddings, config=settings)
        app = SimpleNamespace(state=state)
        gateway = Gateway(app)
        allowed = {stable(f"server:{index}") for index in range(args.services)}
        user = SimpleNamespace(id="fixture-user", auth_version=1, role="admin", scope_mode="all", mcp_ids=[])
        token = SimpleNamespace(id="fixture-token", enable_resource_tools=False, enable_mcp_proposal=False,
                                discovery_mode="discovery")
        async def principal(request, *, target_id=None):
            return user, token, allowed if target_id is None else allowed & {target_id}
        gateway.principal = principal
        request = Request({"type": "http", "method": "POST", "path": "/mcp", "headers": [], "app": app})
        index = catalog.index
        if args.explain:
            from sqlalchemy import event
            plans_seen = set()
            @event.listens_for(index.db.engine.sync_engine, "before_cursor_execute")
            def explain(connection, cursor, statement, parameters, context, many):
                if (not many and statement.lstrip().upper().startswith("SELECT")
                        and "catalog_entries" in statement and statement not in plans_seen):
                    plans_seen.add(statement)
                    cursor.execute("EXPLAIN QUERY PLAN " + statement, parameters)
                    print(json.dumps({"phase": "explain", "sql": statement,
                        "plan": [row[-1] for row in cursor.fetchall()]}), flush=True)
        generations = [stable(f"generation:{number}") for number in range(args.services)]
        profiling = {}
        if args.profile:
            import inspect
            def measure(obj, method, label):
                if not hasattr(obj, method):
                    return
                original = getattr(obj, method)
                def record(started):
                    row = profiling.setdefault(label, {"calls": 0, "ms": 0})
                    row["calls"] += 1
                    row["ms"] += (time.perf_counter() - started) * 1000
                if inspect.iscoroutinefunction(original):
                    async def wrapped(*positional, **keywords):
                        started = time.perf_counter()
                        try:
                            return await original(*positional, **keywords)
                        finally:
                            record(started)
                else:
                    def wrapped(*positional, **keywords):
                        started = time.perf_counter()
                        try:
                            return original(*positional, **keywords)
                        finally:
                            record(started)
                setattr(obj, method, wrapped)
            for obj, names, label in (
                (catalog, ("cached_view", "failure_details", "service_status", "index_scopes", "index_scope", "rows", "prepare_scope", "ensure_ready", "tool_definition", "begin_operation", "record_operation_result", "cached", "save_cache"), "catalog"),
                (catalog.index, ("get_summaries", "get_definition", "_recover_scope", "query_tool_entries", "_entry", "_validate_scope", "_control"), "index"),
                (gateway, ("search_scope", "revalidate_search_scope"), "gateway"),
                (gateway.discovery, ("summary_snapshot", "indexed_tool_search"), "discovery"),
                *(([(gateway.discovery.snapshots, ("get", "prepare", "put", "get_scores", "put_scores"), "snapshots")]) if hasattr(gateway.discovery, "snapshots") else []),
            ):
                for name in names:
                    if hasattr(obj, name):
                        measure(obj, name, label + "." + name)
        patterns = {"rare": "needle000000", "frequent": "weather forecast", "none": "unfindablexyz9911",
                    "typo": "weatherr", "chinese": "天气预报", "exact": "lookupWeather000000"}
        if args.pattern:
            patterns = {args.pattern: patterns[args.pattern]}
        if args.calls_only:
            patterns = {}
        stop, lag = asyncio.Event(), []
        async def heartbeat():
            deadline = asyncio.get_running_loop().time() + .005
            while not stop.is_set():
                await asyncio.sleep(.005)
                stamp = asyncio.get_running_loop().time()
                lag.append(max(0, stamp - deadline) * 1000)
                deadline = stamp + .005
        ticker = asyncio.create_task(heartbeat())
        rss_before = rss()
        try:
            for kind, query in patterns.items():
                for layer in ("store",) if args.store_only else ("store", "gateway"):
                    values, signatures = [], set()
                    for sample in range(args.samples + 1):
                        if sample == 0:
                            index._search_cache.clear()
                            if hasattr(index, "_search_sizes"):
                                index._search_sizes.clear()
                                index._search_cache_bytes = 0
                        started = time.perf_counter()
                        if layer == "store":
                            if kind == "exact":
                                total, rows = await index.query_tool_entries(generations, exact_names=[query], limit=20)
                            else:
                                total, rows = await index.query_tool_entries(generations, query=query, limit=20)
                            signature = tuple(row.gateway_name for row in rows)
                        else:
                            result = await gateway.discovery.search("gateway_search_tools",
                                {"mcp": "*", "tool": query, "limit": 20}, request)
                            body = result.structured_content
                            total, signature = body["total"], tuple(item["gateway_name"] for item in body["tools"])
                            assert all("inputSchema" in item for item in body["tools"])
                        elapsed = (time.perf_counter() - started) * 1000
                        signatures.add(signature)
                        if sample == 0:
                            cold = elapsed
                        else:
                            values.append(elapsed)
                    assert len(signatures) == 1, (kind, layer, "unstable result")
                    assert total == 0 if kind == "none" else total > 0
                    if kind == "exact":
                        assert total == 1 and len(signature) == 1
                    report["queries"][layer + ":" + kind] = {
                        "application_cold_ms": round(cold, 3), **quantiles(values),
                        "total": total, "top20": list(signature), "sample_count": len(values)}
                    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(json.dumps({"phase": "query", "kind": layer + ":" + kind,
                                      **report["queries"][layer + ":" + kind]}, ensure_ascii=False), flush=True)
            if args.purpose_flow:
                values = []
                for sample in range(args.samples + 1):
                    started = time.perf_counter()
                    result = await gateway.discovery.search("gateway_search_mcps",
                        {"query": "weather forecast", "limit": 20}, request)
                    body = result.structured_content
                    assert body["total"] > 0
                    elapsed = (time.perf_counter() - started) * 1000
                    if sample:
                        values.append(elapsed)
                    else:
                        cold = elapsed
                report["queries"]["gateway:service_purpose"] = {
                    "application_cold_ms": round(cold, 3), **quantiles(values),
                    "total": body["total"], "sample_count": len(values)}
                values = []
                for sample in range(args.samples + 1):
                    started = time.perf_counter()
                    result = await gateway.discovery.search("gateway_search_tools",
                        {"mcp": "weather forecast", "tool": "needle000000", "limit": 20}, request)
                    body = result.structured_content
                    assert body["total"] == 1 and len(body["tools"]) == 1
                    assert "inputSchema" in body["tools"][0]
                    elapsed = (time.perf_counter() - started) * 1000
                    if sample:
                        values.append(elapsed)
                    else:
                        cold = elapsed
                report["queries"]["gateway:purpose_then_tool"] = {
                    "application_cold_ms": round(cold, 3), **quantiles(values),
                    "total": body["total"], "sample_count": len(values)}
                print(json.dumps({"phase": "purpose_flow", "queries": {
                    key: value for key, value in report["queries"].items() if "purpose" in key}}), flush=True)
        finally:
            stop.set()
            await ticker
        if not args.store_only and args.services >= 3:
            allowed.clear()
            allowed.update(stable(f"server:{number}") for number in range(2))
            narrowed = (await gateway.discovery.search("gateway_search_tools",
                {"mcp": "*", "tool": "weather forecast", "limit": 20}, request)).structured_content
            assert narrowed["total"] == args.tools * 2
            assert all(item["gateway_name"].startswith(("service-00000__", "service-00001__"))
                       for item in narrowed["tools"])
            user.id = "different-fixture-owner"
            allowed.clear()
            allowed.add(stable("server:2"))
            isolated = (await gateway.discovery.search("gateway_search_tools",
                {"mcp": "*", "tool": "weather forecast", "limit": 20}, request)).structured_content
            assert isolated["total"] == args.tools
            assert all(item["gateway_name"].startswith("service-00002__") for item in isolated["tools"])
            report["acl_checks"] = {"narrowed": narrowed["total"], "different_owner": isolated["total"]}
        if args.call_samples:
            from mcp_manager.logs import LogStore
            logs = LogStore(data)
            catalog.logs = logs
            if args.profile:
                for method in ("append", "_append", "_refresh_deleted", "_index", "_set_offset"):
                    measure(logs, method, "logs." + method)
                if hasattr(logs, "epochs"):
                    for method in ("new_id", "future", "retired"):
                        measure(logs.epochs, method, "epochs." + method)
                if hasattr(logs, "sources"):
                    measure(logs.sources, "generation", "sources.generation")
                if hasattr(logs, "gate"):
                    for method in ("_enter", "_exit"):
                        measure(logs.gate, method, "gate." + method)
                if hasattr(logs, "sources"):
                    for method in ("acquire", "release"):
                        measure(logs.sources.lock, method, "sources.lock." + method)
                if hasattr(logs, "admission"):
                    measure(logs.admission, "_check", "admission._check")
                    reserve = logs.admission.reserve
                    class TimedReservation:
                        def __init__(self, context):
                            self.context = context
                        def timed(self, name, method, *values):
                            started = time.perf_counter()
                            try:
                                return method(*values)
                            finally:
                                item = profiling.setdefault(name, {"calls": 0, "ms": 0})
                                item["calls"] += 1
                                item["ms"] += (time.perf_counter() - started) * 1000
                        def __enter__(self):
                            return self.timed("reservation.enter", self.context.__enter__)
                        def __exit__(self, *values):
                            return self.timed("reservation.exit", self.context.__exit__, *values)
                    logs.admission.reserve = lambda *values, **options: TimedReservation(reserve(*values, **options))
                if hasattr(catalog.cache_store, "artifacts"):
                    measure(catalog.cache_store.artifacts, "_put_json", "compat._put_json")
            dispatches = 0
            async def downstream(spec, lease_id, tool_name, arguments, *, authorize=None):
                nonlocal dispatches
                if authorize:
                    await authorize()
                dispatches += 1
                return {"content": [{"type": "text", "text": "benchmark-result"}], "isError": False}
            runtime.call = downstream
            row = await catalog.get(stable("server:0"))
            lease = SimpleNamespace(id="benchmark-lease", kind="http")
            times = []
            before = (await logs.query(page_size=1))["total"]
            try:
                for sample in range(args.call_samples + 1):
                    started = time.perf_counter()
                    result = await catalog.call(row, lease, "lookupWeather000000", {"reference": "fixture"})
                    assert result["content"][0]["text"] == "benchmark-result" and not result.get("isError")
                    elapsed = (time.perf_counter() - started) * 1000
                    if sample:
                        times.append(elapsed)
                    else:
                        call_cold = elapsed
                assert dispatches == args.call_samples + 1
                assert (await logs.query(page_size=1))["total"] == before + dispatches
                report["catalog_call"] = {
                    "application_cold_ms": round(call_cold, 3), **quantiles(times),
                    "sample_count": len(times), "dispatches": dispatches,
                    "definition": "Real Catalog.call schema, credential preparation, outcome control and durable logs; in-process synthetic downstream, excludes network/runtime scheduling"}
                print(json.dumps({"phase": "call", **report["catalog_call"]}), flush=True)
            finally:
                await logs.close()
        report["event_loop_lag"] = quantiles(lag or [0])
        report["profile"] = profiling
        if profiling:
            print(json.dumps({"phase": "profile", "calls": profiling}), flush=True)
        report["rss_before_bytes"], report["rss_after_bytes"] = rss_before, rss()
        snapshots = getattr(gateway.discovery, "snapshots", None)
        report["directory_cache"] = ({"bytes": snapshots.bytes,
            "entries": [size for _payload, size in snapshots.entries.values()]} if snapshots else None)
        assert runtime.instances == {}, "Discovery started a lazy service"
        report["files"] = {str(path.relative_to(data)): path.stat().st_size
                           for name in ("main.sqlite", "indexes/derived.sqlite", "indexes/logs.sqlite")
                           for suffix in ("", "-wal", "-shm")
                           if (path := data / (name + suffix)).is_file()}
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"phase": "complete", "label": args.label, "queries": len(report["queries"])}), flush=True)
    finally:
        if embeddings is not None and hasattr(embeddings.store, "close"):
            embeddings.store.close()
        if catalog:
            await catalog.close()
        if hasattr(artifacts, "close"):
            artifacts.close()
        await runtime.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
