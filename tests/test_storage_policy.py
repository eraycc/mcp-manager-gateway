"""Capacity settings remain bounded, explicit, and independent from log retention."""
import pytest

from mcp_manager.storage_policy import DEFAULT_STORAGE_POLICY, normalize_storage_policy


def test_partial_policy_keeps_permanent_log_retention_out_of_capacity_limits():
    policy = normalize_storage_policy({"cache_cas_bytes": 8 * 1024**3})
    assert policy["cache_cas_bytes"] == 8 * 1024**3
    assert policy["staging_bytes"] == 512 * 1024**2
    assert "log_retention_days" not in policy
    assert policy["min_free_percent"] == 10
    assert DEFAULT_STORAGE_POLICY["cache_cas_bytes"] == 4 * 1024**3


@pytest.mark.parametrize("value", [
    {"cache_cas_bytes": 0},
    {"cache_cas_bytes": True},
    {"unknown": 1},
    {"min_free_percent": 101},
    {"staging_slots": 0},
    {"wal_warn_bytes": 1024**3, "wal_stop_bytes": 512 * 1024**2},
    {"snapshot_memory_bytes": float("nan")},
    {"durable_cas_bytes": -1},
])
def test_invalid_capacity_policy_fails_before_persistence(value):
    with pytest.raises(ValueError):
        normalize_storage_policy(value)


def test_durable_quota_zero_means_operator_did_not_set_an_additional_hard_limit():
    policy = normalize_storage_policy({"durable_cas_bytes": 0})
    assert policy["durable_cas_bytes"] == 0
    assert policy["min_free_bytes"] > 0
