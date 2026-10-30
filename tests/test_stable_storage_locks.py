"""Stable native lock identities reduce churn; dead stage leases are fully reaped."""
from uuid import uuid4

from filelock import FileLock

from mcp_manager.storage_gate import StorageGate


def test_activity_lock_identity_is_stable_until_its_worker_closes(tmp_path):
    gate = StorageGate(tmp_path)
    try:
        with gate.operation():
            identity = gate.activity_path.stat().st_ino
        assert gate.activity_path.is_file(), "Every operation unlinked and recreated the worker activity lock"
        with gate.operation():
            assert gate.activity_path.stat().st_ino == identity
    finally:
        gate.close()
    assert not gate.activity_path.exists() and not gate.lease_path.exists()


def test_dead_stage_recovery_removes_lease_with_native_persistent_lock_semantics(dual_db, monkeypatch):
    import mcp_manager.storage_admission as module

    admission = module.StorageAdmission(dual_db.settings.data_dir)
    identity, owner = uuid4().hex, uuid4().hex
    lease_path = admission.workers / (owner + ".lock")
    lease_path.write_bytes(b"")
    spool = admission._spool_paths(identity, "cache")[0]
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_bytes(b"abandoned")
    with admission.connect() as connection:
        connection.execute("INSERT INTO storage_stage_reservations(id,owner,namespace,reserved_bytes,created_at) VALUES(?,?,?,?,?)",
                           [identity, owner, "cache", 9, 0])
    def preserved(*args, **kwargs):
        kwargs["preserve_lock_file"] = True
        return FileLock(*args, **kwargs)
    monkeypatch.setattr(module, "FileLock", preserved)
    try:
        assert admission.reap_abandoned()["released"] == 1
        assert not spool.exists()
        assert not lease_path.exists(), "POSIX cleanup left a lease with no reservation to find it again"
    finally:
        admission.close()
