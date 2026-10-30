"""Constant-size append fences; a physically deleted call never needs an eternal tombstone."""
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from .runtime import GatewayError

MAX_EPOCH = 2**32 - 1


class LogEpoch:
    """The caller supplies the shared call-source lock; identity remains a UUID string."""
    def __init__(self, calls_root, lock):
        self.root = Path(calls_root).resolve()
        self.path = self.root / ".sources" / "epochs.json"
        self.lock = lock
        marker = self.path.with_name("epoch-format")
        self.created = False
        with self.lock:
            if not self.path.exists():
                if marker.exists():
                    raise RuntimeError("Missing call epoch metadata; restore consistent log storage")
                self._write({"format": 1, "current": 1, "retired": 0})
                self.created = True
            self._read()
            if not marker.exists():
                with marker.open("xb") as stream:
                    stream.write(b"1\\n")
                    stream.flush()
                    os.fsync(stream.fileno())

    def _read(self):
        with self.path.open("rb") as stream:
            raw = stream.read(1025)
        if len(raw) > 1024:
            raise RuntimeError("Call epoch metadata exceeds its fixed budget")
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"format", "current", "retired"}
                or value["format"] != 1 or type(value["current"]) is not int
                or type(value["retired"]) is not int
                or not 0 <= value["retired"] < value["current"] <= MAX_EPOCH):
            raise RuntimeError("Invalid call epoch metadata; restore consistent log storage before writing")
        return value

    def _write(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.parent / ("." + uuid4().hex + ".epoch.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(value, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def birth(identity):
        if not isinstance(identity, str):
            return None
        try:
            parsed = UUID(identity)
        except ValueError:
            return None
        # A version-8 UUID reserves its high 32 bits for the epoch and keeps
        # 90 random bits. New-ID allocation also checks for an existing row.
        if parsed.version != 8:
            return None
        epoch = parsed.int >> 96
        return epoch if epoch else None

    def new_id(self):
        with self.lock:
            epoch = self._read()["current"]
            value = (uuid4().int & ((1 << 96) - 1)) | (epoch << 96)
            value = (value & ~(15 << 76)) | (8 << 76)
            value = (value & ~(3 << 62)) | (2 << 62)
            return str(UUID(int=value))

    def classify(self, identity):
        birth = self.birth(identity)
        if birth is None:
            return False, False
        with self.lock:
            value = self._read()
            return birth > value["current"], birth <= value["retired"]

    def future(self, identity):
        birth = self.birth(identity)
        with self.lock:
            return birth is not None and birth > self._read()["current"]

    def retired(self, identity):
        birth = self.birth(identity)
        if birth is None:
            return False  # Legacy IDs require the bounded compatibility journal.
        with self.lock:
            return birth <= self._read()["retired"]

    def advance_after_verified_delete(self):
        """Use only after durable source rewrites and projection cutover have completed."""
        with self.lock:
            value = self._read()
            if value["current"] == MAX_EPOCH:
                raise GatewayError("log_epoch_capacity", "Call epoch space exhausted; do not discard deletion decisions")
            value = {"format": 1, "current": value["current"] + 1, "retired": value["current"]}
            self._write(value)
            return value["retired"]
