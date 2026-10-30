"""Staging admission is shared across workers and never evicts persistent files."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_manager.runtime import GatewayError
from mcp_manager.storage_admission import StorageAdmission, migrate_admission_schema
from mcp_manager.storage_policy import DEFAULT_STORAGE_POLICY, MIB


def prepare(tmp_path, **overrides):
    path = tmp_path / "indexes/derived.sqlite"
    path.parent.mkdir()
    policy = DEFAULT_STORAGE_POLICY | overrides
    with sqlite3.connect(path) as connection:
        migrate_admission_schema(connection)
        connection.execute("UPDATE storage_policy_snapshot SET data=? WHERE id=1", [json.dumps(policy)])
    return path


def test_two_workers_share_staging_slots_and_release_after_write(tmp_path, monkeypatch):
    prepare(tmp_path, staging_slots=2, staging_bytes=2 * MIB,
            durable_reserve_slots=0, durable_reserve_bytes=0)
    monkeypatch.setattr("mcp_manager.storage_admission.shutil.disk_usage",
                        lambda _: SimpleNamespace(total=100 * MIB, free=90 * MIB))
    first, second = StorageAdmission(tmp_path), StorageAdmission(tmp_path)
    try:
        first.set_policy(DEFAULT_STORAGE_POLICY | {
            "staging_slots": 2, "staging_bytes": 2 * MIB, "min_free_bytes": 0,
            "min_free_percent": 0, "durable_reserve_slots": 0, "durable_reserve_bytes": 0})
        with first.reserve("cache", MIB), second.reserve("cache", MIB):
            with pytest.raises(GatewayError) as exc, first.reserve("cache", 1):
                raise AssertionError("Capacity exceeded")
            assert exc.value.code == "staging_capacity"
        assert second.usage() == {"slots": 0, "bytes": 0}
    finally:
        first.close()
        second.close()


def test_low_disk_fails_before_creating_a_temp_and_preserves_durable_files(tmp_path, monkeypatch):
    prepare(tmp_path)
    persistent = tmp_path / "keep.json"
    persistent.write_text('{"evidence":"keep"}', encoding="utf-8")
    monkeypatch.setattr("mcp_manager.storage_admission.shutil.disk_usage",
                        lambda _: SimpleNamespace(total=100 * MIB, free=MIB))
    budget = StorageAdmission(tmp_path)
    try:
        with pytest.raises(GatewayError) as exc, budget.reserve("durable", 2 * MIB):
            raise AssertionError("Disk guard did not run before the write")
        assert exc.value.code == "storage_low_space"
        assert persistent.read_text(encoding="utf-8") == '{"evidence":"keep"}'
        assert budget.usage() == {"slots": 0, "bytes": 0}
    finally:
        budget.close()


def hold_reservation(data_dir, ready):
    import time

    budget = StorageAdmission(Path(data_dir))
    try:
        with budget.reserve("cache", MIB):
            ready.set()
            time.sleep(30)
    finally:
        budget.close()


def test_killed_worker_reservation_is_recovered_without_deleting_business_data(tmp_path):
    import multiprocessing

    prepare(tmp_path, min_free_bytes=0, min_free_percent=0)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    worker = context.Process(target=hold_reservation, args=(str(tmp_path), ready))
    worker.start()
    budget = StorageAdmission(tmp_path)
    try:
        assert ready.wait(10), "Child writer did not enter its temporary reservation"
        assert budget.usage()["slots"] == 1
        assert budget.reap_abandoned()["released"] == 0, "Live worker reservation was reclaimed"
        worker.terminate()
        worker.join(10)
        assert not worker.is_alive()
        assert budget.reap_abandoned()["released"] == 1
        assert budget.usage() == {"slots": 0, "bytes": 0}
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(10)
        budget.close()


def test_cache_staging_cannot_consume_the_persistent_domain_reserve(tmp_path, monkeypatch):
    prepare(tmp_path, staging_slots=2, staging_bytes=2 * MIB,
            durable_reserve_slots=1, durable_reserve_bytes=MIB,
            min_free_bytes=0, min_free_percent=0)
    monkeypatch.setattr("mcp_manager.storage_admission.shutil.disk_usage",
                        lambda _: SimpleNamespace(total=100 * MIB, free=90 * MIB))
    budget = StorageAdmission(tmp_path)
    try:
        with budget.reserve("cache", MIB):
            with pytest.raises(GatewayError), budget.reserve("cache", 1):
                raise AssertionError("Cache consumed durable reserve")
            with budget.reserve("durable", MIB):
                assert budget.usage() == {"slots": 2, "bytes": 2 * MIB}
    finally:
        budget.close()
