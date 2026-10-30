"""Daily JSONL is authoritative; SQLite is a disposable query projection."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .catalog import SECRET_KEYS
from .derived_index import prepare_index_path
from .log_index_migrations import migrate_call_index
from .log_sources import LogSources
from .runtime import GatewayError
from .storage_async import join_task, run_blocking


def redact(value, parent=""):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if (key.lower() in SECRET_KEYS or parent in {"env", "headers", "env_headers"}) and item
            else redact(item, key.lower())
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, parent) for item in value]
    return value


LOG_SUMMARY_FIELDS = {
    "id", "event_id", "timestamp", "user_id", "username", "token_id",
    "token_name", "mcp_id", "mcp_name", "tool_name", "status", "source",
    "duration_ms", "_worker_id",
}


def log_summary_and_ref(event, max_preview_bytes=65536):
    """Split oversized redacted events into a bounded query summary and full payload."""
    if type(max_preview_bytes) is not int or max_preview_bytes < 1024:
        raise ValueError("Log preview budget must be at least 1024 bytes")
    value = redact(dict(event))
    raw = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode()
    if len(raw) <= max_preview_bytes:
        return {"summary": value, "payload": None, "payload_bytes": len(raw)}
    summary = {key: value[key] for key in LOG_SUMMARY_FIELDS if key in value}
    summary["payload_truncated"] = True
    summary["payload_bytes"] = len(raw)
    encoded = json.dumps(summary, ensure_ascii=False, default=str, separators=(",", ":")).encode()
    if len(encoded) > max_preview_bytes:
        raise ValueError("Log metadata exceeds the preview budget")
    return {"summary": summary, "payload": value, "payload_bytes": len(raw)}


class LogStore:
    def __init__(self, data_dir: Path, timezone_name="Asia/Shanghai"):
        from .storage_runtime import warn_sqlite_runtime
        warn_sqlite_runtime()
        self.root = Path(data_dir) / "logs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.calls_root = self.root / "calls"
        self.calls_root.mkdir(exist_ok=True)
        self.payload_root = self.calls_root / "payloads"
        self.payload_root.mkdir(exist_ok=True)
        index_path = prepare_index_path(data_dir)
        self.sources = LogSources(self.root, data_dir)
        from .storage_gate import StorageGate
        self.gate = StorageGate(data_dir)
        self.timezone = ZoneInfo(timezone_name)
        self.lock = threading.RLock()
        from filelock import FileLock
        self.cleanup_lock = FileLock(str(Path(data_dir) / "locks/calls-cleanup.lock"), timeout=10)
        self._recovering = True
        self.capacity = asyncio.Semaphore(256)
        self.export_capacity = asyncio.Semaphore(4)
        from .log_projection_ids import DeletedIds
        self.deleted = DeletedIds(self)
        self._deletion_offsets = {}
        self._stats_epoch = 0
        self._stats_cache = OrderedDict()
        self._source_integrity_uncertain = False
        tombstones = self.calls_root / ".deletions"
        tombstones.mkdir(exist_ok=True)
        self.tombstones = tombstones
        self.conn = sqlite3.connect(index_path, check_same_thread=False)
        try:
            with self.cleanup_lock:
                self.conn.row_factory = sqlite3.Row
                self.conn.create_function("local_day", 1, lambda stamp: datetime.fromisoformat(stamp).astimezone(self.timezone).date().isoformat())
                self.conn.execute("PRAGMA foreign_keys=ON")
                self.conn.execute("PRAGMA busy_timeout=5000")
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
                with self.sources.lock:
                    requires_rebuild = migrate_call_index(self.conn)
                    self._refresh_deleted()
                from .log_epoch import LogEpoch
                self.epochs = LogEpoch(self.calls_root, self.sources.lock)
                self.conn.create_function("call_epoch_birth", 1, LogEpoch.birth, deterministic=True)
                # Pending physical deletions are completed before offsets can be served.
                if self.deleted:
                    with self.lock, self.gate.operation(), self.sources.lock:
                        self._rewrite_deleted(self.deleted)
                self._rebuild(full=requires_rebuild)
                if self.epochs.created and self.conn.execute(
                        "SELECT 1 FROM calls WHERE call_epoch_birth(id) IS NOT NULL LIMIT 1").fetchone():
                    raise RuntimeError("Missing call epoch metadata for existing calls; restore it before writing")
                self._release_deleted_payloads()
                self._compact_deletions(cold_start=True)
                self._recovering = False
                from .storage_admission import StorageAdmission
                self.admission = StorageAdmission(data_dir, gate=self.gate)
                from .audit_store import AuditStore

                self.audits = AuditStore(self)
        except BaseException:
            if hasattr(self, "admission"):
                self.admission.close()
            self.conn.close()
            self.sources.close()
            self.gate.close()
            raise

    def _refresh_deleted(self):
        """Consume durable deletion decisions before every writer mutates a source."""
        paths = [*(self.root / "deletions").glob("*.jsonl"),
                 *self.tombstones.glob("*.jsonl")]
        live = set(paths)
        for missing in self._deletion_offsets.keys() - live:
            del self._deletion_offsets[missing]
        for path in paths:
            stat = path.stat()
            saved = self.conn.execute("SELECT inode,offset FROM call_deletion_sources WHERE path=?",
                                      [path.relative_to(self.root).as_posix()]).fetchone()
            inode, offset = tuple(saved) if saved else (None, 0)
            if inode != str(stat.st_ino) or offset > stat.st_size:
                offset = 0
            with path.open("rb") as stream:
                stream.seek(offset)
                while raw := stream.readline():
                    if not raw.endswith(b"\n"):
                        break
                    try:
                        self.deleted.update(json.loads(raw)["ids"])
                    except (ValueError, KeyError, TypeError):
                        pass
                    offset = stream.tell()
            self.conn.execute("INSERT OR REPLACE INTO call_deletion_sources VALUES(?,?,?)",
                              [path.relative_to(self.root).as_posix(), str(stat.st_ino), offset])

    async def append(self, event):
        async with self.capacity:
            return await run_blocking(self._append, event)

    def _append(self, event):
        event = redact(dict(event))
        event["event_id"] = uuid.uuid4().hex
        event.setdefault("timestamp", datetime.now(UTC).isoformat())
        event["timestamp"] = self.normalize_time(event["timestamp"], default_timezone=UTC)
        event.setdefault("source", "gateway")
        event.setdefault("duration_ms", 0)
        event["_worker_id"] = self.sources.worker_id
        stamp = datetime.fromisoformat(event["timestamp"])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        path = self.calls_root / (stamp.astimezone(self.timezone).strftime("%Y-%m-%d") + ".jsonl")
        with self.lock, self.gate.operation(), self.sources.lock:
            self._refresh_deleted()
            self.conn.commit()
            if "id" not in event:
                for _ in range(8):
                    event["id"] = self.epochs.new_id()
                    if not self.conn.execute("SELECT 1 FROM calls WHERE id=?", [event["id"]]).fetchone():
                        break
                else:
                    raise GatewayError("call_id_capacity", "Could not allocate a unique call identity")
            if event["id"] in self.deleted:
                return event["id"]
            future, retired = self.epochs.classify(event["id"])
            if future:
                raise GatewayError("call_epoch_invalid", "Call identity belongs to an unknown future epoch")
            if retired:
                # An unrelated call may have remained in flight across cleanup.
                # Only its original live owner can complete that existing row.
                running = self.conn.execute(
                    "SELECT calls.status,worker_id FROM calls JOIN call_owners ON call_id=calls.id WHERE calls.id=?",
                    [event["id"]]).fetchone()
                if not running or running["status"] != "running" or running["worker_id"] != self.sources.worker_id:
                    return event["id"]
            separated = log_summary_and_ref(event)
            if separated["payload"] is not None:
                payload_raw = json.dumps(
                    separated["payload"], ensure_ascii=False, default=str,
                    separators=(",", ":")).encode("utf-8")
                payload_ref = self._write_payload(payload_raw)
                event = separated["summary"] | {"_payload_ref": payload_ref}
            raw = (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode("utf-8")
            with self.admission.reserve("calls", max(4096, len(raw))), path.open("a+b") as f:
                if f.tell():
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        f.seek(0, os.SEEK_END)
                        f.write(b"\n")
                f.seek(0, os.SEEK_END)
                offset = f.tell()
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            # The source lock covers this entire append and projection, so
            # no rewriter can replace the source between these steps. Read its
            # generation once; delayed/rebuild projections retain their own
            # explicit generation fence in _index.
            generation = self.sources.generation(path)
            name = self.sources.name(path)
            self._index(event, path, offset, len(raw), _source_name=name)
            self._set_offset(name, offset + len(raw))
            self.conn.execute("INSERT OR REPLACE INTO call_source_versions VALUES(?,?)",
                              [name, generation])
            self.conn.commit()
            self._invalidate_stats()
        return event["id"]

    def _payload_path(self, identity):
        if not isinstance(identity, str) or len(identity) != 64:
            raise ValueError("Invalid log payload reference")
        if any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Invalid log payload reference")
        return self.payload_root / identity[:2] / identity[2:4] / (identity + ".json")

    def _write_payload(self, raw):
        identity = hashlib.sha256(raw).hexdigest()
        target = self._payload_path(identity)
        with self.lock, self.gate.operation(), self.sources.lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                return {"id": identity, "size": len(raw)}
            with self.admission.reserve("calls", max(4096, len(raw))) as reservation:
                temporary = self.calls_root / ("." + reservation + ".tmp")
                try:
                    with temporary.open("xb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        return {"id": identity, "size": len(raw)}

    def _read_payload(self, reference):
        with reference["path"].open("rb") as source:
            raw = source.read(reference["size"] + 1)
        if len(raw) != reference["size"]:
            raise RuntimeError("Log payload size changed")
        return json.loads(raw)

    def _payload_reference_path(self, value):
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("Invalid payload path")
        if len(candidate.parts) > 1:
            return self.root / candidate
        identity = candidate.stem
        modern = self._payload_path(identity)
        if modern.exists():
            return modern
        return self.root / "payloads" / (identity + ".json")

    def _index(self, event, path, offset, size, *, source_generation=None, _source_name=None):
        if source_generation is not None and source_generation != self.sources.generation(path):
            return False
        if event["id"] in self.deleted:
            return False
        source_name = _source_name if _source_name is not None else self.sources.name(path)
        fields = ("timestamp", "user_id", "username", "token_id", "token_name", "mcp_id",
                  "mcp_name", "tool_name", "status", "source", "duration_ms")
        self.conn.execute("INSERT OR REPLACE INTO calls VALUES(" + ",".join("?" * 15) + ")",
                          [event["id"], source_name, offset, size] + [
                              self.normalize_time(event["timestamp"], default_timezone=UTC)
                              if f == "timestamp" else event.get(f) for f in fields])
        self.conn.execute("INSERT OR IGNORE INTO call_event_sources VALUES(?,?)", [event["id"], source_name])
        payload = event.get("_payload_ref")
        if isinstance(payload, dict):
            self.conn.execute(
                "INSERT OR REPLACE INTO call_payload_roots(path,offset,artifact_id,payload_path,size,log_id) VALUES(?,?,?,?,?,?)",
                [source_name, offset, payload["id"],
                 self._payload_reference_path(payload["id"] + ".json").relative_to(self.root).as_posix(),
                 payload["size"], event["id"]])
            self.conn.execute(
                "INSERT OR REPLACE INTO payloads(log_id,artifact_id,path,size) VALUES(?,?,?,?)",
                [
                    event["id"],
                    payload["id"],
                    self._payload_reference_path(payload["id"] + ".json").relative_to(self.root).as_posix(),
                    payload["size"],
                ],
            )
        else:
            self.conn.execute("DELETE FROM payloads WHERE log_id=?", [event["id"]])
        self.conn.execute("INSERT OR REPLACE INTO call_owners(call_id,worker_id) VALUES(?,?)",
                          [event["id"], event.get("_worker_id")])
        return True

    def _set_offset(self, path, offset):
        self.conn.execute(
            "INSERT INTO index_offsets(path,offset) VALUES(?,?) "
            "ON CONFLICT(path) DO UPDATE SET offset=excluded.offset",
            [path, offset],
        )

    def _rebuild(self, *, full=False, paths=None):
        with self.lock, self.gate.operation(), self.sources.lock:
            self._refresh_deleted()
            if full:
                self._source_integrity_uncertain = False
                self.conn.execute("DELETE FROM calls")
                self.conn.execute("DELETE FROM index_offsets")
                self.conn.execute("DELETE FROM payloads")
                self.conn.execute("DELETE FROM call_owners")
                self.conn.execute("DELETE FROM call_source_versions")
                self.conn.execute("DELETE FROM call_event_sources")
                self.conn.execute("DELETE FROM call_payload_roots")
            partial = paths is not None
            paths = self.sources.paths() if paths is None else paths
            names = {self.sources.name(path) for path in paths if path.exists()}
            indexed_names = {
                row[0] for row in self.conn.execute("SELECT path FROM index_offsets")
            }
            removed = (indexed_names & {self.sources.name(path) for path in paths}) - names if partial else indexed_names - names
            for name in removed:
                self.conn.execute("DELETE FROM calls WHERE path=?", [name])
                self.conn.execute("DELETE FROM index_offsets WHERE path=?", [name])
                self.conn.execute("DELETE FROM call_event_sources WHERE path=?", [name])
                self.conn.execute("DELETE FROM call_payload_roots WHERE path=?", [name])
            self.conn.execute("DELETE FROM calls WHERE id IN (SELECT id FROM call_deletions)")
            self.conn.execute("DELETE FROM call_owners WHERE call_id IN (SELECT id FROM call_deletions)")
            self.conn.execute("DELETE FROM payloads WHERE log_id IN (SELECT id FROM call_deletions)")
            for path in paths:
                if not path.exists():
                    continue
                name = self.sources.name(path)
                generation = self.sources.generation(path)
                saved = self.conn.execute(
                    "SELECT offset,generation FROM index_offsets LEFT JOIN call_source_versions USING(path) WHERE path=?", [name]
                ).fetchone()
                start = saved["offset"] if saved else 0
                size = path.stat().st_size
                if not saved or start > size or saved["generation"] != generation:
                    self.conn.execute("DELETE FROM calls WHERE path=?", [name])
                    self.conn.execute("DELETE FROM call_event_sources WHERE path=?", [name])
                    self.conn.execute("DELETE FROM call_payload_roots WHERE path=?", [name])
                    start = 0
                committed = start
                with path.open("rb") as stream:
                    stream.seek(start)
                    while True:
                        offset = stream.tell()
                        raw = stream.readline()
                        if not raw:
                            break
                        if not raw.endswith(b"\n"):
                            break
                        committed = stream.tell()
                        try:
                            event = json.loads(raw)
                            if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                                self._source_integrity_uncertain = True
                                continue
                            self.normalize_time(event["timestamp"], default_timezone=UTC)
                        except (ValueError, KeyError, TypeError):
                            self._source_integrity_uncertain = True
                            continue
                        # Projection failures are not malformed input. Let startup
                        # fail rather than silently skipping acknowledged events.
                        self._index(event, path, offset, len(raw), source_generation=generation)
                self._set_offset(name, committed)
                self.conn.execute("INSERT OR REPLACE INTO call_source_versions VALUES(?,?)", [name, generation])
            running = self.conn.execute(
                "SELECT calls.id,call_owners.worker_id FROM calls LEFT JOIN call_owners ON calls.id=call_owners.call_id "
                "WHERE calls.status='running'").fetchall()
            for item in running:
                if not self.sources.owner_alive(item["worker_id"]):
                    self.conn.execute("UPDATE calls SET status='outcome_unknown' WHERE id=?", [item["id"]])
            self.conn.commit()
            self._invalidate_stats()

    def _invalidate_stats(self):
        self._stats_epoch += 1
        self._stats_cache.clear()

    async def rebuild(self):
        await run_blocking(self._rebuild, full=True)

    @staticmethod
    def normalize_time(value, *, default_timezone):
        try:
            stamp = datetime.fromisoformat(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Log time must be a valid ISO 8601 date/time") from exc
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=default_timezone)
        return stamp.astimezone(UTC).isoformat(timespec="microseconds")

    def _where(self, filters):
        sql, values = [], []
        for key in ("user_id", "token_id", "mcp_id", "tool_name", "status", "source"):
            if filters.get(key) is not None and filters[key] != "":
                sql.append(key + " = ?")
                values.append(filters[key])
        if filters.get("username"):
            username = str(filters["username"]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql.append("LOWER(COALESCE(username,'')) LIKE LOWER(?) ESCAPE '\\'")
            values.append("%" + username + "%")
        if filters.get("q"):
            sql.append("(COALESCE(username,'') || ' ' || COALESCE(token_name,'') || ' ' || "
                       "COALESCE(mcp_name,'') || ' ' || COALESCE(tool_name,'') LIKE ?)")
            values.append("%" + filters["q"] + "%")
        for key, op in (("from_time", ">="), ("to_time", "<=")):
            if filters.get(key):
                sql.append("timestamp " + op + " ?")
                values.append(self.normalize_time(filters[key], default_timezone=self.timezone))
        return (" WHERE " + " AND ".join(sql)) if sql else "", values

    async def query(self, *, page=1, page_size=20, **filters):
        return await run_blocking(self._query, page, page_size, filters)

    def _query(self, page, page_size, filters):
        where, values = self._where(filters)
        page_size = max(1, min(int(page_size), 200))
        with self.lock:
            total = self.conn.execute("SELECT count(*) FROM calls" + where, values).fetchone()[0]
            pages = max(1, math.ceil(total / page_size))
            page = max(1, min(int(page), pages))
            rows = self.conn.execute("SELECT * FROM calls" + where +
                " ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
                values + [page_size, (page - 1) * page_size]).fetchall()
            items = [
                {key: value for key, value in dict(row).items() if key not in ("path", "offset", "size")}
                for row in rows
            ]
        return {"items": items, "total": total, "page": page, "page_size": page_size, "total_pages": pages}

    async def reference(self, id, *, user_id=None):
        return await run_blocking(self._reference, id, user_id)

    def _reference(self, id, user_id):
        with self.lock, self.gate.operation(), self.sources.lock:
            row = self.conn.execute("SELECT * FROM calls WHERE id=?", [id]).fetchone()
            if not row or user_id is not None and row["user_id"] != user_id:
                return None
            payload = self.conn.execute(
                "SELECT artifact_id,path,size FROM payloads WHERE log_id=?",
                [id],
            ).fetchone()
            return {
                "id": row["id"],
                "path": self._payload_reference_path(payload["path"]) if payload else self.root / row["path"],
                "offset": 0 if payload else row["offset"],
                "size": payload["size"] if payload else row["size"],
                "artifact_id": payload["artifact_id"] if payload else None,
                "user_id": row["user_id"],
                "source_path": row["path"], "source_offset": row["offset"],
                "source_size": row["size"],
                "source_generation": self.sources.generation(self.root / row["path"]),
            }

    async def preview(self, id, *, user_id=None, max_bytes=65536):
        reference = await self.reference(id, user_id=user_id)
        if not reference:
            return None
        def read():
            with self._open_reference(reference) as source:
                return source.read(min(reference["size"], max_bytes))
        raw = await run_blocking(read)
        truncated = reference["size"] > max_bytes
        return {
            "preview": raw[:max_bytes].decode("utf-8", errors="ignore"),
            "truncated": truncated,
            "size_bytes": reference["size"],
        }

    def _open_reference(self, reference):
        # Open a stable descriptor while the source fence is held. POSIX keeps
        # its inode alive; Windows defers replacement while this handle is open.
        with self.lock, self.gate.operation(), self.sources.lock:
            current = self._reference(reference["id"], reference["user_id"])
            fields = ("source_path", "source_offset", "source_size", "source_generation", "artifact_id", "size")
            if current is None or any(current[key] != reference[key] for key in fields):
                raise GatewayError("log_reference_changed", "Log source changed; retry this read-only download")
            source = current["path"].open("rb")
        try:
            if current["artifact_id"]:
                digest = hashlib.sha256()
                while raw := source.read(65536):
                    digest.update(raw)
                legacy = current["path"].parent == self.root / "payloads"
                if source.tell() != current["size"] or not legacy and digest.hexdigest() != current["artifact_id"]:
                    raise GatewayError("log_payload_corrupt", "Log payload integrity verification failed")
            else:
                source.seek(current["offset"])
                event = json.loads(source.read(current["size"]))
                if event.get("id") != current["id"] or event.get("user_id") != current["user_id"]:
                    raise GatewayError("log_reference_changed", "Log source changed; retry this read-only download")
            source.seek(current["offset"])
            return source
        except BaseException:
            source.close()
            raise

    async def download_chunks(self, reference, *, chunk_size=65536):
        opening = asyncio.create_task(asyncio.to_thread(self._open_reference, reference))
        try:
            source = await asyncio.shield(opening)
        except asyncio.CancelledError:
            try:
                source = await join_task(opening)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001,S110 - keep owner cancellation after joining
                pass
            else:
                await run_blocking(source.close)
            raise
        remaining = reference["size"]
        try:
            while remaining:
                chunk = await run_blocking(source.read, min(chunk_size, remaining))
                if not chunk:
                    raise GatewayError("log_payload_corrupt", "Log source ended before its recorded size")
                remaining -= len(chunk)
                yield chunk
        finally:
            await run_blocking(source.close)

    async def detail(self, id, *, user_id=None):
        return await run_blocking(self._detail, id, user_id)

    def _decode_reference(self, reference):
        # The descriptor is pinned under the source fence; decoding runs after
        # releasing it so a large management read cannot stall call writers.
        with self._open_reference(reference) as source:
            event = json.loads(source.read(reference["size"]))
        if event.get("id") != reference["id"] or event.get("user_id") != reference["user_id"]:
            raise RuntimeError("Log index does not match source; rebuild required")
        return event

    def _detail(self, id, user_id):
        with self.lock, self.gate.operation(), self.sources.lock:
            reference = self._reference(id, user_id)
            if reference is None:
                return None
            status = self.conn.execute("SELECT status FROM calls WHERE id=?", [id]).fetchone()[0]
        event = self._decode_reference(reference)
        event["status"] = status
        return event

    async def export_batch(self, filters, *, cursor=None, limit=100):
        return await run_blocking(self._export_batch, filters, cursor, min(200, max(1, limit)))

    def _export_batch(self, filters, cursor, limit):
        where, values = self._where(filters)
        if cursor:
            where += (" AND " if where else " WHERE ") + "(timestamp < ? OR (timestamp = ? AND id < ?))"
            values += [cursor[0], cursor[0], cursor[1]]
        with self.lock, self.gate.operation(), self.sources.lock:
            rows = self.conn.execute("SELECT id,user_id,timestamp,status FROM calls" + where +
                " ORDER BY timestamp DESC, id DESC LIMIT ?", values + [limit]).fetchall()
            references = [(row, self._reference(row["id"], row["user_id"])) for row in rows]
        result = []
        for row, reference in references:
            if reference is None:
                raise GatewayError("log_reference_changed", "Log source changed; retry this read-only export")
            event = self._decode_reference(reference)
            event["status"] = row["status"]
            result.append(event)
        next_cursor = (rows[-1]["timestamp"], rows[-1]["id"]) if rows else None
        return result, next_cursor

    async def delete(self, ids=None, **filters):
        return await run_blocking(self._delete, ids, filters)

    def _delete(self, ids, filters):
        with self.cleanup_lock, self.gate.operation():
            return self._delete_serial(ids, filters)

    def _delete_serial(self, ids, filters):
        with self.lock, self.sources.lock:
            self._refresh_deleted()
            where, args = self._where(filters)
            if ids is not None:
                self.conn.execute("DELETE FROM call_delete_requests")
                self.conn.executemany("INSERT OR IGNORE INTO call_delete_requests VALUES(?)", ((id,) for id in ids))
                where += (" AND " if where else " WHERE ") + "id IN (SELECT id FROM call_delete_requests)"
            count = self.conn.execute("SELECT count(*) FROM calls" + where, args).fetchone()[0]
            if not count:
                for path in self.sources.paths():
                    if path.stat().st_size == 0:
                        path.unlink()
                self.conn.execute("DELETE FROM call_delete_requests")
                self.conn.commit()
                return 0
            from .storage_policy import normalize_storage_policy
            policy = normalize_storage_policy(json.loads(self.conn.execute(
                "SELECT data FROM storage_policy_snapshot WHERE id=1").fetchone()[0]))
            legacy_where = where + (" AND " if where else " WHERE ") + "call_epoch_birth(id) IS NULL"
            legacy = self.conn.execute("SELECT count(*) FROM calls" + legacy_where, args).fetchone()[0]
            existing = self.conn.execute(
                "SELECT count(*) FROM call_deletions WHERE call_epoch_birth(id) IS NULL").fetchone()[0]
            if legacy + existing > policy["legacy_call_tombstones"]:
                self.conn.execute("DELETE FROM call_delete_requests")
                self.conn.commit()
                raise GatewayError("legacy_tombstone_capacity",
                    "Legacy deletion fence budget reached; restart all workers to retire old callbacks, or increase its explicit budget")
            journal = self.tombstones / (uuid.uuid4().hex + ".jsonl")
            temporary = journal.with_suffix(".tmp")
            try:
                # Streaming durable intent avoids an all-matching Python set or
                # a SQLite variable per target. Each recovery record is bounded.
                with temporary.open("x", encoding="utf-8") as stream:
                    cursor = self.conn.execute("SELECT id FROM calls" + where + " ORDER BY id", args)
                    while batch := cursor.fetchmany(250):
                        stream.write(json.dumps({"ids": [row[0] for row in batch]}) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, journal)
            finally:
                temporary.unlink(missing_ok=True)
            self._refresh_deleted()
            self.conn.execute("""INSERT OR IGNORE INTO call_delete_payload_candidates
                SELECT artifact_id,payload_path FROM call_payload_roots
                WHERE log_id IN (SELECT id FROM call_deletions)""")
            self.conn.execute("DELETE FROM call_delete_requests")
            for table, column in (("calls", "id"), ("call_owners", "call_id"), ("payloads", "log_id")):
                self.conn.execute("DELETE FROM " + table + " WHERE " + column + " IN (SELECT id FROM call_deletions)")
            self.conn.commit()
            self._invalidate_stats()
        # Durable decisions are visible to every writer before the long copy.
        self._rewrite_deleted(self.deleted)
        with self.lock, self.sources.lock:
            self._release_deleted_payloads()
            self.epochs.advance_after_verified_delete()
            self._compact_modern_deletions()
        return count

    def _release_deleted_payloads(self):
        previous = ""
        while batch := self.conn.execute(
                "SELECT artifact_id,path FROM call_delete_payload_candidates WHERE artifact_id>? ORDER BY artifact_id LIMIT 250",
                [previous]).fetchall():
            previous = batch[-1]["artifact_id"]
            for payload in batch:
                if not self.conn.execute("SELECT 1 FROM call_payload_roots WHERE artifact_id=? LIMIT 1",
                                         [payload["artifact_id"]]).fetchone():
                    self._payload_reference_path(payload["path"]).unlink(missing_ok=True)
                self.conn.execute("DELETE FROM call_delete_payload_candidates WHERE artifact_id=?", [payload["artifact_id"]])
            self.conn.commit()

    def _rewrite_deleted(self, selected):
        if self._recovering:
            return self._rewrite_deleted_recovery(selected)
        from .call_rewrite import CallRewriter
        with self.lock, self.sources.lock:
            names = [row[0] for row in self.conn.execute(
                "SELECT DISTINCT path FROM call_event_sources WHERE id IN (SELECT id FROM call_deletions) ORDER BY path")]
        rewriter = CallRewriter(self)
        for name in names:
            rewriter.rewrite(self.root / name)
        return [self.root / name for name in names]

    def _rewrite_deleted_recovery(self, selected):
        names = [row[0] for row in self.conn.execute(
            "SELECT DISTINCT path FROM call_event_sources WHERE id IN (SELECT id FROM call_deletions) ORDER BY path")]
        # A lost projection has no trustworthy source-to-ID map yet. Recovery
        # must inspect durable sources before it can retire deletion decisions.
        paths = [self.root / name for name in names] if names else self.sources.paths()
        changed = []
        for path in paths:
            if not path.exists():
                changed.append(path)
                continue
            temp = path.with_suffix(".rewrite")
            try:
                with path.open("rb") as source, temp.open("wb") as target:
                    for raw in source:
                        try:
                            keep = json.loads(raw).get("id") not in selected
                        except (ValueError, TypeError, AttributeError):
                            keep = True
                        if keep:
                            target.write(raw)
                    target.flush()
                    os.fsync(target.fileno())
                # Fence every old offset before changing or removing its source.
                self.sources.bump(path)
                if temp.stat().st_size:
                    os.replace(temp, path)
                else:
                    path.unlink()
                changed.append(path)
            finally:
                temp.unlink(missing_ok=True)
        return changed

    def _compact_modern_deletions(self):
        # The epoch floor is durable before replacing any per-ID decisions.
        self.conn.execute("DELETE FROM call_deletions WHERE call_epoch_birth(id) IS NOT NULL")
        journal = self.tombstones / "legacy.jsonl"
        temporary = journal.with_suffix(".tmp")
        paths = [*(self.root / "deletions").glob("*.jsonl"), *self.tombstones.glob("*.jsonl")]
        count = 0
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                cursor = self.conn.execute("SELECT id FROM call_deletions ORDER BY id")
                while batch := cursor.fetchmany(250):
                    count += len(batch)
                    stream.write(json.dumps({"ids": [row[0] for row in batch]}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if count:
                os.replace(temporary, journal)
            else:
                journal.unlink(missing_ok=True)
            for path in paths:
                if path != journal:
                    path.unlink(missing_ok=True)
            self.conn.execute("DELETE FROM call_deletion_sources")
            if count:
                stat = journal.stat()
                self.conn.execute("INSERT INTO call_deletion_sources VALUES(?,?,?)",
                                  [journal.relative_to(self.root).as_posix(), str(stat.st_ino), stat.st_size])
            self.conn.commit()
        finally:
            temporary.unlink(missing_ok=True)
        self._deletion_offsets.clear()

    def _compact_deletions(self, *, cold_start=False):
        # At cold start this worker has no old queued callbacks or active calls.
        # Live peers keep the decisions until their source work has quiesced.
        if not cold_start:
            return 0
        with self.lock, self.gate.operation(), self.sources.lock:
            for path in self.sources.workers.glob("*.lock"):
                if path.stem != self.sources.worker_id and self.sources.owner_alive(path.stem):
                    return 0
            paths = [*(self.root / "deletions").glob("????-??-??.jsonl"),
                     *self.tombstones.glob("*.jsonl")]
            for path in paths:
                path.unlink()
            self.deleted.clear()
            self.conn.execute("DELETE FROM call_deletion_sources")
            self._deletion_offsets.clear()
            self.conn.commit()
            return len(paths)

    async def stats(self, *, user_id=None, token_id=None):
        return await run_blocking(self._stats, user_id, token_id)

    def _stats(self, user_id, token_id):
        cache_key = (user_id, token_id)
        current = time.monotonic()
        with self.lock:
            version = (self._stats_epoch, self.conn.execute("PRAGMA data_version").fetchone()[0])
            cached = self._stats_cache.get(cache_key)
            if cached and cached[0] > current and cached[1] == version:
                return copy.deepcopy(cached[2])
            where, args = self._where({"user_id": user_id, "token_id": token_id, "source": "gateway"})
            rows = self.conn.execute("SELECT status,count(*) n FROM calls" + where + " GROUP BY status", args)
            counts = {r["status"]: r["n"] for r in rows}
            daily = [dict(r) for r in self.conn.execute(
                "SELECT local_day(timestamp) day,count(*) calls,"
                "sum(CASE WHEN status='success' THEN 1 ELSE 0 END) success "
                "FROM calls" + where + " GROUP BY day ORDER BY day DESC LIMIT 30", args)]
            result = {"calls": sum(v for k, v in counts.items() if k != "running"),
                      "success": counts.get("success", 0),
                      "failed": sum(counts.get(k, 0) for k in ("tool_error", "gateway_error", "auth_rejected")),
                      "unknown": counts.get("outcome_unknown", 0),
                      "cancelled": counts.get("cancelled", 0), "by_status": counts,
                      "daily": list(reversed(daily))}
            self._stats_cache[cache_key] = (current + 10.0, version, result)
            self._stats_cache.move_to_end(cache_key)
            while len(self._stats_cache) > 512:
                self._stats_cache.popitem(last=False)
            return copy.deepcopy(result)

    async def token_counts(self, ids):
        def query():
            if not ids:
                return {}
            with self.lock:
                rows = self.conn.execute(
                    "SELECT token_id,count(*) call_count,sum(status='success') success_count,"
                    "sum(status IN ('tool_error','gateway_error','auth_rejected')) failed_count FROM calls "
                    "WHERE source='gateway' AND status!='running' AND token_id IN (" +
                    ",".join("?" for _ in ids) + ") GROUP BY token_id", ids)
                return {row["token_id"]: {k: row[k] for k in ("call_count", "success_count", "failed_count")}
                        for row in rows}
        return await run_blocking(query)

    async def audit(self, action, user_id, details, **context):
        return await self.audits.append(action, user_id, details, **context)

    async def close(self):
        await run_blocking(self.audits.close)
        await run_blocking(self.admission.close)
        with self.lock, self.gate.operation(), self.sources.lock:
            if getattr(self, "_orphan_cursor", None) is not None:
                self._orphan_cursor.close()
                self._orphan_cursor = None
            self.conn.close()
            self.sources.close()
        self.gate.close()
