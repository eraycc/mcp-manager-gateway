"""File accounting is bounded, content-free, and cannot follow storage symlinks."""
import pytest

from mcp_manager.storage_inventory import StorageInventory


def test_inventory_advances_in_bounded_units_and_distinguishes_domains(tmp_path):
    paths = {
        "objects/cache/sha256/aa/bb/" + "a" * 64 + ".json": b"cache",
        "objects/durable/sha256/cc/dd/" + "c" * 64 + ".json": b"durable",
        "logs/calls/2026-10-30.jsonl": b"call",
        "logs/audit/2026-10-30.jsonl": b"audit",
        "backups/storage/run/main.sqlite": b"backup",
        "secret.key": b"do-not-read-this-secret",
    }
    for name, content in paths.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    inventory = StorageInventory(tmp_path)
    for _ in range(100):
        progress = inventory.scan_next(limit=2)
        assert progress["units"] <= 2
        if progress["complete"]:
            break
    assert progress["complete"]
    assert progress["domains"]["cache_objects"] == {"files": 1, "bytes": 5}
    assert progress["domains"]["durable_objects"] == {"files": 1, "bytes": 7}
    assert progress["domains"]["calls"]["bytes"] == 4
    assert progress["domains"]["audit"]["bytes"] == 5
    assert progress["domains"]["backups"]["bytes"] == 6
    assert progress["domains"]["configuration"]["files"] == 1
    assert "do-not-read-this-secret" not in str(progress)


def test_inventory_skips_external_symlinks_and_closes_active_walk_on_reset(tmp_path):
    root = tmp_path / "managed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.json").write_bytes(b"not-managed")
    try:
        (root / "objects").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Filesystem does not permit creating a test symlink")
    inventory = StorageInventory(root)
    for _ in range(20):
        progress = inventory.scan_next(limit=2)
        if progress["complete"]:
            break
    assert progress["complete"] and progress["skipped"] >= 1
    assert sum(value["bytes"] for value in progress["domains"].values()) == 0
    inventory.reset()
    assert not inventory.complete


def test_inventory_counts_a_custom_primary_path_once_and_only_reads_its_metadata(tmp_path):
    path = tmp_path / "custom.sqlite"
    path.write_bytes(b"protected-database")
    inventory = StorageInventory(tmp_path, primary_path=path)
    for _ in range(20):
        report = inventory.scan_next(limit=2)
        if report["complete"]:
            break
    assert report["domains"]["main"] == {"files": 1, "bytes": len(b"protected-database")}
