"""A killed writer leaves only owned temporary files eligible for exact recovery."""
import multiprocessing
import time
from pathlib import Path

import pytest
from test_storage_admission import prepare

from mcp_manager.storage_admission import StorageAdmission


def hold_spool(root, ready, namespace):
    budget = StorageAdmission(root)
    with budget.reserve(namespace, 4096) as identity:
        folder = Path(root) / ("objects/cache/sha256" if namespace == "cache" else "logs/" + namespace)
        path = folder / ("." + identity + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        budget.set_path(identity, path)
        path.write_bytes(b"partial owned spool")
        if namespace == "calls":
            with budget.connect() as connection:
                connection.execute("CREATE TABLE call_rewrite_offsets(rewrite_id TEXT,old_offset INTEGER,new_offset INTEGER)")
                connection.execute("INSERT INTO call_rewrite_offsets VALUES(?,0,0)", [identity])
        ready.set()
        time.sleep(30)


@pytest.mark.parametrize("namespace", ["cache", "calls", "audit"])
def test_killed_spool_is_physically_removed_but_other_files_are_preserved(tmp_path, namespace):
    prepare(tmp_path, min_free_bytes=0, min_free_percent=0)
    unrelated = tmp_path / "keep.tmp"
    unrelated.write_bytes(b"unmanaged")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    worker = context.Process(target=hold_spool, args=(str(tmp_path), ready, namespace))
    worker.start()
    budget = StorageAdmission(tmp_path)
    try:
        assert ready.wait(10)
        folder = tmp_path / ("objects/cache/sha256" if namespace == "cache" else "logs/" + namespace)
        owned = list(folder.glob("*.tmp"))
        assert len(owned) == 1
        assert budget.reap_abandoned()["released"] == 0
        assert owned[0].is_file()
        worker.terminate()
        worker.join(10)
        assert not worker.is_alive()
        assert budget.reap_abandoned()["released"] == 1
        assert not owned[0].exists()
        if namespace == "calls":
            with budget.connect() as connection:
                assert connection.execute("SELECT count(*) FROM call_rewrite_offsets").fetchone()[0] == 0
        assert unrelated.read_bytes() == b"unmanaged"
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(10)
        budget.close()
