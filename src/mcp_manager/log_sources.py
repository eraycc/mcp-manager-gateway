"""Daily source identities, rewrite fencing and same-host writer ownership."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from filelock import FileLock, Timeout


class LogSources:
    def __init__(self, root, data_dir):
        self.root = Path(root).resolve()
        lock_root = Path(data_dir).resolve() / "locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(lock_root / "calls-sources.lock"), timeout=10)
        self.metadata = self.root / "calls" / ".sources"
        self.metadata.mkdir(parents=True, exist_ok=True)
        self.workers = lock_root / "call-workers"
        self.workers.mkdir(exist_ok=True)
        self.worker_id = uuid.uuid4().hex
        self.worker_path = self.workers / (self.worker_id + ".lock")
        self.worker_lease = FileLock(str(self.worker_path), timeout=0)
        self.worker_lease.acquire()

    def name(self, path):
        candidate = Path(path).resolve()
        relative = candidate.relative_to(self.root).as_posix()
        if not re.fullmatch(r"(?:calls/)?\d{4}-\d{2}-\d{2}\.jsonl", relative):
            raise ValueError("Invalid call source path")
        return relative

    def paths(self):
        legacy = self.root.glob("????-??-??.jsonl")
        current = (self.root / "calls").glob("????-??-??.jsonl")
        return sorted([*legacy, *current], key=lambda p: (p.name, p.parent != self.root))

    def _metadata_path(self, path):
        identity = hashlib.sha256(self.name(path).encode()).hexdigest()
        return self.metadata / (identity + ".json")

    def generation(self, path):
        target = self._metadata_path(path)
        with self.lock:
            if target.exists():
                value = json.loads(target.read_text(encoding="utf-8"))
                if value.get("path") != self.name(path) or not re.fullmatch("[0-9a-f]{32}", value.get("generation", "")):
                    raise RuntimeError("Invalid call source generation metadata")
                return value["generation"]
            return self.bump(path)

    def bump(self, path):
        target = self._metadata_path(path)
        generation = uuid.uuid4().hex
        value = {"path": self.name(path), "generation": generation}
        temporary = target.with_name("." + uuid.uuid4().hex + ".tmp")
        with self.lock:
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(value, stream, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return generation

    def owner_alive(self, owner):
        if owner == self.worker_id:
            return True
        if not isinstance(owner, str) or not re.fullmatch("[0-9a-f]{32}", owner):
            return False
        path = self.workers / (owner + ".lock")
        if not path.exists():
            return False
        lease = FileLock(str(path), timeout=0)
        try:
            lease.acquire()
        except Timeout:
            return True
        else:
            lease.release()
            return False

    def close(self):
        self.worker_lease.release()
        self.worker_path.unlink(missing_ok=True)
