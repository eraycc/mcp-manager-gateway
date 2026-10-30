"""Validated storage budgets; age retention stays in the existing calls/audit settings."""
import math

STORAGE_POLICY_KEY = "storage_governance"
MIB = 1024**2
GIB = 1024**3

DEFAULT_STORAGE_POLICY = {
    "cache_cas_bytes": 4 * GIB,
    "durable_cas_bytes": 0,
    "catalog_sql_bytes": 4 * GIB,
    "retiring_sql_bytes": 2 * GIB,
    "staging_bytes": 512 * MIB,
    "staging_slots": 32,
    "durable_reserve_bytes": 64 * MIB,
    "durable_reserve_slots": 4,
    "min_free_bytes": GIB,
    "min_free_percent": 10,
    "wal_warn_bytes": 128 * MIB,
    "wal_stop_bytes": 512 * MIB,
    "query_cache_bytes": 256 * MIB,
    "query_cache_rows": 100000,
    "query_cache_ttl_seconds": 86400,
    "snapshot_memory_bytes": 64 * MIB,
    "artifact_memory_bytes": 128 * MIB,
    "gc_cycle_milliseconds": 2000,
    "gc_row_budget": 50000,
    "gc_interval_seconds": 30,
    "legacy_call_tombstones": 100000,
}

_LIMITS = {
    "cache_cas_bytes": (MIB, 2**50),
    "durable_cas_bytes": (0, 2**50),
    "catalog_sql_bytes": (MIB, 2**50),
    "retiring_sql_bytes": (MIB, 2**50),
    "staging_bytes": (MIB, 2**40),
    "staging_slots": (1, 1024),
    "durable_reserve_bytes": (0, 2**40),
    "durable_reserve_slots": (0, 1024),
    "min_free_bytes": (0, 2**50),
    "wal_warn_bytes": (MIB, 2**40),
    "wal_stop_bytes": (MIB, 2**40),
    "query_cache_bytes": (MIB, 2**40),
    "query_cache_rows": (100, 10000000),
    "query_cache_ttl_seconds": (60, 604800),
    "snapshot_memory_bytes": (0, 2 * GIB),
    "artifact_memory_bytes": (0, 2 * GIB),
    "gc_cycle_milliseconds": (100, 5000),
    "gc_row_budget": (250, 50000),
    "gc_interval_seconds": (5, 300),
    "legacy_call_tombstones": (1, 1000000),
}


def normalize_storage_policy(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_STORAGE_POLICY):
        raise ValueError("Unknown storage governance setting")
    result = DEFAULT_STORAGE_POLICY | value
    for key, (minimum, maximum) in _LIMITS.items():
        number = result[key]
        if type(number) is not int or not minimum <= number <= maximum:
            raise ValueError(f"{key} must be an integer from {minimum} to {maximum}")
    percent = result["min_free_percent"]
    if (isinstance(percent, bool) or not isinstance(percent, (int, float))
            or not math.isfinite(percent) or not 0 <= percent <= 95):
        raise ValueError("min_free_percent must be finite and from 0 to 95")
    if (result["durable_reserve_bytes"] > result["staging_bytes"]
            or result["durable_reserve_slots"] > result["staging_slots"]):
        raise ValueError("Durable staging reserve must fit inside the staging budget")
    if result["wal_stop_bytes"] < result["wal_warn_bytes"]:
        raise ValueError("WAL stop threshold must be at least its warning threshold")
    return result
