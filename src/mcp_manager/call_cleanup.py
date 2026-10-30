"""Small metadata-only sweeps of unreferenced call bodies and abandoned fences."""
import hashlib
import json
import re

from filelock import FileLock, Timeout


def _paths(owner):
    for root in (owner.payload_root, owner.root / "payloads"):
        yield from root.glob("*/*/*.json")
        yield from root.glob("*.json")
    yield from owner.sources.metadata.glob("*.json")
    yield from owner.sources.workers.glob("*.lock")


def collect_call_orphans(owner, *, limit=100):
    if type(limit) is not int or not 1 <= limit <= 250:
        raise ValueError("Invalid call cleanup budget")
    removed, scanned = 0, 0
    with owner.lock, owner.gate.operation(), owner.sources.lock:
        if getattr(owner, "_orphan_cursor", None) is None:
            owner._orphan_cursor = _paths(owner)
        for _ in range(limit):
            path = next(owner._orphan_cursor, None)
            if path is None:
                owner._orphan_cursor.close()
                owner._orphan_cursor = None
                break
            scanned += 1
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(owner.root.parent.resolve()):
                    continue
                if path.parent == owner.sources.metadata:
                    if not re.fullmatch("[0-9a-f]{64}", path.stem):
                        continue  # Epoch and format metadata are never garbage.
                    with path.open("rb") as source:
                        raw = source.read(1025)
                    if len(raw) > 1024:
                        continue
                    value = json.loads(raw)
                    name = value.get("path")
                    if not isinstance(name, str) or hashlib.sha256(name.encode()).hexdigest() != path.stem:
                        continue
                    source_path = owner.root / name
                    owner.sources.name(source_path)  # Exact permitted date path.
                    if source_path.exists():
                        continue
                    if owner.conn.execute("SELECT 1 FROM index_offsets WHERE path=?", [name]).fetchone():
                        continue
                    path.unlink()
                    removed += 1
                elif path.parent == owner.sources.workers:
                    if not re.fullmatch("[0-9a-f]{32}", path.stem) or path.stem == owner.sources.worker_id:
                        continue
                    lease = FileLock(str(path), timeout=0)
                    try:
                        lease.acquire()
                    except Timeout:
                        continue
                    else:
                        lease.release()
                        path.unlink(missing_ok=True)
                        removed += 1
                else:
                    if not re.fullmatch("[0-9a-f]{64}", path.stem):
                        continue
                    if getattr(owner, "_source_integrity_uncertain", False):
                        continue
                    rooted = owner.conn.execute("""SELECT 1 FROM call_payload_roots WHERE artifact_id=?
                        UNION ALL SELECT 1 FROM payloads WHERE artifact_id=? LIMIT 1""", [path.stem, path.stem]).fetchone()
                    if not rooted:
                        path.unlink()
                        removed += 1
            except (FileNotFoundError, PermissionError, ValueError, TypeError):
                continue
    return {"scanned": scanned, "deleted": removed}
