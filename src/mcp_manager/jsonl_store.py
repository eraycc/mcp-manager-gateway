"""Single-process JSONL snapshots shared by cache and background-job storage."""
import copy
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


def windows_retry(operation):
    for attempt in range(6):
        try:
            return operation()
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 5:
                raise
            time.sleep(.01 * 2 ** attempt)


class JsonlStore:
    """Append updates, retain one current value per key, compact superseded rows."""
    def __init__(self, path, *, fail_closed=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.items = {}
        self.records = 0
        self.lock = threading.RLock()
        corrupt = False
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        key = record["key"]
                        if not isinstance(key, str) or "value" not in record:
                            raise ValueError("Invalid JSONL snapshot")
                        self._apply(key, record["value"])
                        self.records += 1
                    except (ValueError, KeyError, TypeError):
                        corrupt = True
            if corrupt:
                log.warning("Incomplete or invalid record in %s", self.path)
                if fail_closed:
                    self.items.clear()  # A torn invalidation must not resurrect cached tools.
                self.compact()

    def _apply(self, key, value):
        if value is None:
            self.items.pop(key, None)
        else:
            self.items[key] = value

    def get(self, key):
        with self.lock:
            return copy.deepcopy(self.items.get(key))

    def _append(self, raw):
        # A leading newline isolates a partial record from a previous failed write.
        with self.path.open("ab") as stream:
            stream.write(b"\n" + raw)
            stream.flush()
            os.fsync(stream.fileno())

    def set(self, key, value):
        raw = (json.dumps({"key": key, "value": value}, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self.lock:
            windows_retry(lambda: self._append(raw))
            self._apply(key, copy.deepcopy(value))
            self.records += 1
            if self.records > max(128, len(self.items) * 3):
                try:
                    self.compact()
                except OSError:
                    # The update is already durable. Compaction may retry later.
                    log.warning("JSONL compaction deferred for %s", self.path, exc_info=True)

    def compact(self):
        temp = self.path.with_name(self.path.stem + ".compact.jsonl")
        with self.lock:
            try:
                with temp.open("wb") as stream:
                    for key, value in self.items.items():
                        stream.write((json.dumps({"key": key, "value": value}, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                windows_retry(lambda: temp.replace(self.path))
                self.records = len(self.items)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    log.warning("Unable to remove JSONL compaction file %s", temp)

    def delete_prefix(self, prefix):
        with self.lock:
            for key in list(self.items):
                if key.startswith(prefix):
                    self.set(key, None)

    def migrate_json(self, convert):
        """Import before removing each old snapshot; unreadable files are preserved."""
        for path in self.path.parent.glob("*.json"):
            try:
                key, value = convert(path, json.loads(path.read_text(encoding="utf-8")))
                existing = self.get(key)
                if existing is None or ("revision" in value and
                                        existing.get("revision", -1) < value["revision"]):
                    self.set(key, value)
                path.unlink()
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("Legacy snapshot could not be migrated: %s", path, exc_info=True)
