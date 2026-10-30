"""Admission protects real CAS spools, including failures and domain sharing."""
from types import SimpleNamespace

import pytest

from mcp_manager.artifact_store import ArtifactStore
from mcp_manager.runtime import GatewayError
from mcp_manager.storage_policy import MIB


def test_cas_checks_disk_before_creating_spool_and_releases_failed_write(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path)
    try:
        monkeypatch.setattr("mcp_manager.storage_admission.shutil.disk_usage",
                            lambda _: SimpleNamespace(total=100 * MIB, free=MIB))
        with pytest.raises(GatewayError) as exc:
            store._put_json({"value": "keep"}, 8 * MIB)
        assert exc.value.code == "storage_low_space"
        assert not list(store.root.rglob("*.tmp"))
        assert store.admission.usage() == {"slots": 0, "bytes": 0}
    finally:
        store.close()


def test_cas_spools_grow_by_bytes_written_not_the_callers_maximum(tmp_path):
    store = ArtifactStore(tmp_path)
    persistent = store.for_namespace("durable")
    try:
        store.admission.set_policy({
            "staging_bytes": MIB, "durable_reserve_bytes": 0,
            "min_free_bytes": 0, "min_free_percent": 0})
        ref = persistent._put_json({"small": True}, 256 * MIB)
        assert ref.size_bytes == 14
        assert store.admission is persistent.admission
        assert store.admission.usage() == {"slots": 0, "bytes": 0}
        with pytest.raises(GatewayError) as exc:
            store._put_json({"body": "x" * (2 * MIB)}, 8 * MIB)
        assert exc.value.code == "staging_capacity"
        assert not list(store.root.rglob("*.tmp"))
        assert store.admission.usage() == {"slots": 0, "bytes": 0}
        assert persistent._path(ref.id).is_file()
    finally:
        persistent.close()
        store.close()


def test_policy_cache_quota_does_not_evict_or_block_durable_domain(tmp_path):
    store = ArtifactStore(tmp_path)
    persistent = store.for_namespace("durable")
    try:
        store.admission.set_policy({
            "cache_cas_bytes": MIB, "min_free_bytes": 0, "min_free_percent": 0})
        cached = store._put_json({"body": "x" * (MIB // 2)}, 2 * MIB)
        with pytest.raises(GatewayError) as exc:
            store._put_json({"body": "y" * (MIB // 2)}, 2 * MIB)
        assert exc.value.code == "cache_capacity"
        durable = persistent._put_json({"body": "z" * MIB}, 2 * MIB)
        assert store._path(cached.id).is_file()
        assert persistent._path(durable.id).is_file()
        assert store.admission.usage()["slots"] == 0
    finally:
        persistent.close()
        store.close()
