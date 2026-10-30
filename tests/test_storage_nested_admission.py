"""A declined maintenance barrier cannot block nested work already admitted by its owner."""
import threading

from mcp_manager.storage_gate import StorageGate


def test_nested_admitted_work_does_not_reenter_a_declined_global_barrier(tmp_path):
    worker, maintenance = StorageGate(tmp_path), StorageGate(tmp_path)
    entered = threading.Event()
    def nested():
        with worker.operation():
            entered.set()
    thread = None
    try:
        with worker.operation():
            with maintenance.maintenance() as admitted:
                assert not admitted
                thread = threading.Thread(target=nested)
                thread.start()
                progressed = entered.wait(.3)
            thread.join(5)
            assert progressed, "Nested admitted work waited for a global file lock it already supersedes"
        with maintenance.maintenance() as admitted:
            assert admitted
    finally:
        if thread is not None:
            thread.join(5)
        worker.close()
        maintenance.close()
