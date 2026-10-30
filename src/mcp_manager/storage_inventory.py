"""Bounded, metadata-only accounting of managed storage trees."""
import copy
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

ROOTS = ("objects", "logs", "indexes", "catalog", "cache", "jobs", "backups")
POINTS = ("secret.key", ".env", "mcp-manager.sqlite", "mcp-manager.sqlite-wal", "mcp-manager.sqlite-shm")


def domain(relative):
    parts = relative.parts
    name = parts[-1]
    if name.endswith((".tmp", ".rewrite")) or name.startswith(".") and name.endswith(".jsonl"):
        return "temporary"
    if parts[0] in POINTS[:2]:
        return "configuration"
    if parts[0].startswith("mcp-manager.sqlite"):
        return "main"
    if parts[0] == "objects":
        if len(parts) > 1 and parts[1] in {"cache", "durable"}:
            return parts[1] + "_objects"
        return "legacy_objects"
    if parts[0] == "logs":
        if len(parts) > 1 and parts[1] == "audit":
            return "audit"
        if "payloads" in parts:
            return "call_payloads"
        if ".deletions" in parts or "deletions" in parts:
            return "call_deletions"
        if ".sources" in parts:
            return "call_sources_metadata"
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.jsonl", name):
            return "calls"
        return "other_logs"
    return {"indexes": "indexes", "catalog": "manifests", "cache": "legacy_cache",
            "jobs": "legacy_jobs", "backups": "backups"}.get(parts[0], "other")


class StorageInventory:
    def __init__(self, data_dir, *, primary_path=None):
        self.root = Path(data_dir).resolve()
        self.primary_path = Path(primary_path).resolve() if primary_path is not None else None
        self.cursor = None
        self.complete = False
        self.domains = {}
        self.skipped = 0
        self.started_at = None
        self.last_complete = None

    def reset(self):
        if self.cursor is not None:
            self.cursor.close()
        self.cursor = None
        self.complete = False
        self.domains = {}
        self.skipped = 0
        self.started_at = None

    def _walk(self):
        def tree(path, depth):
            yield None  # Directory work counts too, even if it contains no files.
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                    yield "skip"
                    return
                if path.is_file():
                    yield path, path.stat().st_size
                    return
                if depth >= 12:
                    yield "skip"
                    return
                with os.scandir(path) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            yield "skip"
                        elif entry.is_dir(follow_symlinks=False):
                            yield from tree(Path(entry.path), depth + 1)
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path), entry.stat(follow_symlinks=False).st_size
                        else:
                            yield "skip"
            except FileNotFoundError:
                return  # A concurrent collector may remove an unreferenced file.
            except OSError:
                yield "skip"
        for name in (*ROOTS, *POINTS):
            path = self.root / name
            if path.exists() or path.is_symlink():
                yield from tree(path, 0)
        primary_is_scanned = (self.primary_path is not None and self.primary_path.is_relative_to(self.root)
                              and (self.primary_path.relative_to(self.root).parts[0] in ROOTS
                                   or self.primary_path.relative_to(self.root).as_posix() in POINTS))
        if self.primary_path is not None and not primary_is_scanned:
            # A configured custom primary is an exact protected file, never
            # an invitation to walk its parent database or user directory.
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(self.primary_path) + suffix)
                try:
                    if path.is_file() and not path.is_symlink():
                        yield "configured_primary", path.stat().st_size
                except OSError:
                    yield "skip"

    def scan_next(self, *, limit=1000, max_seconds=.1):
        if type(limit) is not int or not 1 <= limit <= 5000 or not 0 < max_seconds <= 2:
            raise ValueError("Invalid storage inventory budget")
        if self.complete:
            return self._report(0)
        if self.cursor is None:
            self.cursor = self._walk()
            self.started_at = datetime.now(UTC).isoformat()
        deadline, units = time.monotonic() + max_seconds, 0
        while units < limit and time.monotonic() < deadline:
            item = next(self.cursor, "complete")
            if item == "complete":
                self.complete = True
                self.cursor.close()
                self.cursor = None
                self.last_complete = datetime.now(UTC).isoformat()
                break
            units += 1
            if item is None:
                continue
            if item == "skip":
                self.skipped += 1
                continue
            path, size = item
            primary_paths = {Path(str(self.primary_path) + suffix) for suffix in ("", "-wal", "-shm")} if self.primary_path else set()
            key = "main" if path == "configured_primary" or path in primary_paths else domain(path.relative_to(self.root))
            values = self.domains.setdefault(key, {"files": 0, "bytes": 0})
            values["files"] += 1
            values["bytes"] += size
        return self._report(units)

    def _report(self, units):
        return {"units": units, "complete": self.complete, "started_at": self.started_at,
                "completed_at": self.last_complete if self.complete else None,
                "skipped": self.skipped, "domains": copy.deepcopy(self.domains)}
