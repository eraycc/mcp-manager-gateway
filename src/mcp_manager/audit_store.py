"""Durable audit JSONL with a rebuildable index and recoverable physical deletion."""
import asyncio
import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta

from filelock import FileLock, Timeout

from .audit_events import build_event, normalize, timestamp
from .log_index_migrations import migrate_audit_index
from .storage_async import run_blocking

LOGGER = logging.getLogger(__name__)
COLUMNS = ("event_id", "timestamp", "action", "category", "actor_type", "user_id", "username",
           "target_type", "target_id", "target_name", "result", "source", "ip", "request_id")
FILTERS = {"action", "category", "user_id", "username", "actor_type", "target_type", "target_id",
           "result", "source", "ip", "q", "from_time", "to_time"}
MAX_LINE = 1024 * 1024


class AuditStore:
    def __init__(self, owner):
        self.owner = owner
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(owner.root.parent / "indexes/derived.sqlite", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.source_lock = FileLock(str(owner.root.parent / "locks" / "audit-sources.lock"), timeout=10)
        self.root = owner.root / "audit"
        self.root.mkdir(exist_ok=True)
        self.journals = self.root / ".deletions"
        self.journals.mkdir(exist_ok=True)
        self.write_failures = self.index_failures = 0
        self.last_error = None
        self.consecutive_write_failures = 0
        self.last_write_success_at = None
        self.skipped = {}
        try:
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            with self.lock, self.owner.gate.operation(), self.source_lock:
                migrate_audit_index(self.conn)
                self._recover()
                self._sync()
        except BaseException:
            self.conn.close()
            raise

    def close(self):
        with self.lock:
            self.conn.close()

    def _error(self, exc, *, projection=False):
        import errno

        code = getattr(exc, "code", None)
        number = getattr(exc, "errno", None)
        if isinstance(exc, Timeout):
            reason, hint = "lock_timeout", "A storage lock was busy; retry after active work completes."
        elif number == errno.ENOSPC:
            reason, hint = "disk_full", "Free disk space or adjust retention."
        elif number in {errno.EACCES, errno.EPERM}:
            reason, hint = "permission_denied", "Check data-directory ownership and write permissions."
        elif isinstance(exc, sqlite3.Error):
            reason = "index_busy" if "locked" in str(exc).lower() else "index_error"
            hint = "Inspect derived-index diagnostics."
        elif code in {"storage_busy", "staging_capacity", "storage_low_space", "wal_pressure"}:
            reason, hint = code, "Inspect storage governance and active maintenance."
        else:
            reason, hint = type(exc).__name__, "Inspect the server error type and filesystem health."
        if projection:
            self.index_failures += 1
        else:
            self.write_failures += 1
            self.consecutive_write_failures += 1
        # Never persist arbitrary exception text: it can contain credentials.
        self.last_error = {"type": type(exc).__name__, "timestamp": timestamp(), "projection": projection,
                           "reason": reason, "phase": getattr(exc, "audit_phase", "event_preparation"),
                           "errno": number, "hint": hint}
        LOGGER.error("Audit %s failed: reason=%s phase=%s errno=%s",
                     "projection" if projection else "persistence", reason, self.last_error["phase"], number)

    async def append(self, action, user_id, details, *, require=False, **context):
        async with self.owner.capacity:
            try:
                event = build_event(action, user_id, details, **context)
                return await run_blocking(self._append, event)
            except Exception as exc:
                self._error(exc)
                if require:
                    raise
                return None

    def _write(self, event):
        stamp = datetime.fromisoformat(event["timestamp"])
        path = self.root / stamp.astimezone(self.owner.timezone).strftime("%Y-%m-%d.jsonl")
        raw = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with self.owner.admission.reserve("audit", max(4096, len(raw))), path.open("a+b") as stream:
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    stream.seek(0, os.SEEK_END)
                    stream.write(b"\n")
            stream.seek(0, os.SEEK_END)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return event["event_id"]

    def _append(self, event):
        phase = "local_writer"
        try:
            with self.lock:
                phase = "maintenance_admission"
                with self.owner.gate.operation():
                    phase = "source_lock"
                    with self.source_lock:
                        phase = "jsonl_write"
                        identity = self._write(event)
                        self.consecutive_write_failures = 0
                        self.last_write_success_at = timestamp()
                        try:
                            self._sync()
                        except Exception as exc:  # noqa: BLE001 -- durable JSONL recovers on the next index pass.
                            self.conn.rollback()
                            exc.audit_phase = "index_projection"
                            self._error(exc, projection=True)
                        return identity
        except Exception as exc:
            exc.audit_phase = phase
            raise

    def _event(self, raw, path, offset):
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError("Audit event must be an object")
        if not isinstance(value.get("timestamp"), str) or not value["timestamp"]:
            raise ValueError("Raw audit event must have its original timestamp")
        if not isinstance(value.get("action"), str) or not value["action"]:
            raise ValueError("Raw audit event must have an action")
        digest = hashlib.sha256(path.name.encode() + str(offset).encode() + raw).hexdigest()
        return normalize(value, fallback_id=digest)

    def _index(self, event, path, offset, size, scan_id):
        preview = event["details"]
        if event["action"] == "mcp.stop" and preview.get("instance_id"):
            preview = {key: str(preview[key])[:64] for key in ("reason_code", "instance_id", "lease_id")
                       if preview.get(key) is not None}
        elif event["action"] == "lease.disconnect" and preview.get("reason_code") in {
                "lease_timeout", "bridge_heartbeat_timeout"}:
            ids = preview.get("instance_ids", [])
            preview = {"reason_code": preview["reason_code"], "instance_count": len(ids) if isinstance(ids, list) else 0}
        summary = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))[:300]
        fields = COLUMNS + ("summary", "path", "offset", "size", "scan_id")
        values = [event.get(key) for key in COLUMNS] + [summary, path.name, offset, size, scan_id]
        updates = ",".join(key + "=excluded." + key for key in fields if key != "event_id")
        self.conn.execute("INSERT INTO audit_events(" + ",".join(fields) + ") VALUES(" +
                          ",".join("?" for _ in fields) + ") ON CONFLICT(event_id) DO UPDATE SET " + updates, values)

    def _sync(self, *, full=False):
        paths = sorted(self.root.glob("????-??-??.jsonl"))
        names = {path.name for path in paths}
        indexed = {row[0] for row in self.conn.execute("SELECT path FROM audit_index_offsets")}
        for name in indexed - names:
            self.conn.execute("DELETE FROM audit_events WHERE path=?", [name])
            self.conn.execute("DELETE FROM audit_index_offsets WHERE path=?", [name])
            self.skipped.pop(name, None)
        for path in paths:
            info = path.stat()
            saved = self.conn.execute("SELECT offset,mtime_ns,skipped FROM audit_index_offsets WHERE path=?", [path.name]).fetchone()
            rescan = full or saved is None or saved["offset"] > info.st_size or (
                saved["offset"] == info.st_size and saved["mtime_ns"] != info.st_mtime_ns)
            start = 0 if rescan else saved["offset"]
            if not rescan and start == info.st_size:
                continue
            scan_id = uuid.uuid4().hex
            skipped = 0 if rescan else saved["skipped"]
            committed = start
            indexed_batch = 0
            with path.open("rb") as stream:
                stream.seek(start)
                while True:
                    offset = stream.tell()
                    raw = stream.readline(MAX_LINE + 1)
                    if not raw:
                        break
                    if len(raw) > MAX_LINE:
                        while raw and not raw.endswith(b"\n"):
                            raw = stream.readline(MAX_LINE + 1)
                        if raw.endswith(b"\n"):
                            committed = stream.tell()
                            skipped += 1
                            continue
                        break
                    if not raw.endswith(b"\n"):
                        break
                    committed = stream.tell()
                    try:
                        self._index(self._event(raw, path, offset), path, offset, len(raw), scan_id)
                        indexed_batch += 1
                        if indexed_batch == 250:
                            self.conn.commit()
                            indexed_batch = 0
                    except (ValueError, KeyError, TypeError):
                        skipped += 1
            if rescan:
                self.conn.execute("DELETE FROM audit_events WHERE path=? AND scan_id!=?", [path.name, scan_id])
            self.conn.execute("INSERT INTO audit_index_offsets(path,offset,mtime_ns,skipped) VALUES(?,?,?,?) "
                              "ON CONFLICT(path) DO UPDATE SET offset=excluded.offset,mtime_ns=excluded.mtime_ns,skipped=excluded.skipped",
                              [path.name, committed, info.st_mtime_ns, skipped])
            self.skipped[path.name] = skipped
        self.conn.commit()

    def _where(self, filters, *, max_seq=None, cursor=None):
        parts, values = [], []
        for key in FILTERS - {"q", "from_time", "to_time", "username"}:
            if filters.get(key) not in (None, ""):
                parts.append(key + "=?")
                values.append(filters[key])
        for key, column in (("q", ("action || ' ' || COALESCE(username,'') || ' ' || COALESCE(target_name,'') || ' ' || "
                                  "COALESCE(target_id,'') || ' ' || COALESCE(summary,'')")), ("username", "COALESCE(username,'')")):
            if filters.get(key):
                value = str(filters[key]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                parts.append("LOWER(" + column + ") LIKE LOWER(?) ESCAPE '\\'")
                values.append("%" + value + "%")
        for key, op in (("from_time", ">="), ("to_time", "<=")):
            if filters.get(key):
                parts.append("timestamp" + op + "?")
                values.append(self.owner.normalize_time(filters[key], default_timezone=self.owner.timezone))
        if max_seq is not None:
            parts.append("seq<=?")
            values.append(max_seq)
        if cursor:
            parts.append("(timestamp,event_id)<(?,?)")
            values.extend(cursor)
        return (" WHERE " + " AND ".join(parts) if parts else ""), values

    async def query(self, *, page=1, page_size=20, **filters):
        return await asyncio.to_thread(self._query, page, page_size, filters)

    def _query(self, page, page_size, filters):
        with self.lock, self.owner.gate.operation(), self.source_lock:
            self._sync()
            where, args = self._where(filters)
            total = self.conn.execute("SELECT COUNT(*) FROM audit_events" + where, args).fetchone()[0]
            pages = max(1, math.ceil(total / page_size))
            page = min(page, pages)
            rows = self.conn.execute("SELECT * FROM audit_events" + where +
                " ORDER BY timestamp DESC,event_id DESC LIMIT ? OFFSET ?", args + [page_size, (page - 1) * page_size]).fetchall()
            return {"items": [self._summary(row) for row in rows], "total": total, "page": page,
                    "page_size": page_size, "total_pages": pages}

    @staticmethod
    def _summary(row):
        return {"id": row["event_id"], **{key: row[key] for key in (*COLUMNS, "summary")}}

    def _detail(self, row):
        if row is None:
            return None
        path = self.root / row["path"]
        with path.open("rb") as stream:
            stream.seek(row["offset"])
            raw = stream.read(row["size"])
        return self._event(raw, path, row["offset"])

    async def detail(self, identity):
        def read():
            with self.lock, self.owner.gate.operation(), self.source_lock:
                self._sync()
                return self._detail(self.conn.execute("SELECT * FROM audit_events WHERE event_id=?", [identity]).fetchone())
        return await asyncio.to_thread(read)

    async def watermark(self):
        def read():
            with self.lock, self.owner.gate.operation(), self.source_lock:
                self._sync()
                return self.conn.execute("SELECT COALESCE(MAX(seq),0) FROM audit_events").fetchone()[0]
        return await asyncio.to_thread(read)

    async def export_batch(self, *, max_seq, cursor=None, limit=200, **filters):
        def read():
            with self.lock, self.owner.gate.operation(), self.source_lock:
                self._sync()
                where, args = self._where(filters, max_seq=max_seq, cursor=cursor)
                rows = self.conn.execute("SELECT * FROM audit_events" + where +
                    " ORDER BY timestamp DESC,event_id DESC LIMIT ?", args + [limit]).fetchall()
                return [self._detail(row) for row in rows], ((rows[-1]["timestamp"], rows[-1]["event_id"]) if rows else None)
        return await asyncio.to_thread(read)

    async def status(self):
        def read():
            with self.lock, self.owner.gate.operation(), self.source_lock:
                self._sync()
                row = self.conn.execute("SELECT COUNT(*),MIN(timestamp),MAX(timestamp) FROM audit_events").fetchone()
                files = list(self.root.glob("????-??-??.jsonl"))
                return {"events": row[0], "oldest": row[1], "newest": row[2], "files": len(files),
                        "bytes": sum(path.stat().st_size for path in files), "write_failures": self.write_failures,
                        "index_failures": self.index_failures, "last_error": self.last_error,
                        "consecutive_write_failures": self.consecutive_write_failures,
                        "last_write_success_at": self.last_write_success_at,
                        "skipped_records": self.conn.execute("SELECT COALESCE(SUM(skipped),0) FROM audit_index_offsets").fetchone()[0],
                        "pending_deletions": len(list(self.journals.glob("*.jsonl")))}
        return await asyncio.to_thread(read)

    async def rebuild(self):
        def run():
            with self.lock, self.owner.gate.operation(), self.source_lock:
                self._recover()
                self._sync(full=True)
        await run_blocking(run)

    def _rewrite_targets(self, operation_id):
        from .storage_admission import StorageAdmission
        paths = self.conn.execute("SELECT DISTINCT path FROM audit_delete_targets WHERE operation_id=?", [operation_id]).fetchall()
        self.conn.commit()
        admission = StorageAdmission(self.owner.root.parent, gate=self.owner.gate)
        try:
            for row in paths:
                path = self.root / row[0]
                if not path.exists():
                    continue
                with admission.reserve("audit", 4096) as reservation:
                    temporary = self.root / ("." + reservation + ".tmp")
                    try:
                        with path.open("rb") as source, temporary.open("xb") as target:
                            pending = bytearray()
                            def flush(pending=pending):
                                if pending:
                                    admission.grow(reservation, max(4096, target.tell() + len(pending)))
                                    target.write(pending)
                                    pending.clear()
                            while True:
                                offset = source.tell()
                                raw = source.readline(MAX_LINE + 1)
                                if not raw:
                                    break
                                if len(raw) > MAX_LINE:
                                    # Preserve an unknown oversized record verbatim
                                    # without materializing the whole record in RAM.
                                    while True:
                                        pending.extend(raw)
                                        flush()
                                        if raw.endswith(b"\n"):
                                            break
                                        raw = source.readline(MAX_LINE + 1)
                                        if not raw:
                                            break
                                    continue
                                try:
                                    identity = self._event(raw, path, offset)["event_id"]
                                except (ValueError, KeyError, TypeError):
                                    identity = None
                                deleted = identity and self.conn.execute("SELECT 1 FROM audit_delete_targets WHERE "
                                    "operation_id=? AND event_id=?", [operation_id, identity]).fetchone()
                                if not deleted:
                                    pending.extend(raw)
                                    if len(pending) >= 262144:
                                        flush()
                            flush()
                            target.flush()
                            os.fsync(target.fileno())
                        if temporary.stat().st_size:
                            os.replace(temporary, path)
                        else:
                            path.unlink()
                        self.conn.execute("DELETE FROM audit_index_offsets WHERE path=?", [path.name])
                        self.conn.execute("DELETE FROM audit_events WHERE path=? AND event_id IN "
                            "(SELECT event_id FROM audit_delete_targets WHERE operation_id=?)", [path.name, operation_id])
                        self.conn.commit()
                    finally:
                        temporary.unlink(missing_ok=True)
        finally:
            admission.close()

    def _recover(self):
        for journal in sorted(self.journals.glob("*.jsonl")):
            operation_id = journal.stem
            with journal.open(encoding="utf-8") as stream:
                metadata = json.loads(next(stream))
                for number, raw in enumerate(stream, 1):
                    target = json.loads(raw)
                    # Journal paths are generated locally and must remain direct day files.
                    if len(target["path"]) != 16 or "/" in target["path"] or "\\" in target["path"]:
                        raise ValueError("Invalid audit deletion journal")
                    self.conn.execute("INSERT OR IGNORE INTO audit_delete_targets VALUES(?,?,?)",
                                      [operation_id, target["event_id"], target["path"]])
                    if number % 250 == 0:
                        self.conn.commit()
            self._rewrite_targets(operation_id)
            self._sync(full=True)
            self._append(build_event(metadata["action"], metadata["actor"].get("user_id"), {
                "operation_id": operation_id, "phase": "recovered", "count": metadata["count"],
                "files": metadata["files"]}, **{key: value for key, value in metadata["actor"].items() if key != "user_id"}))
            self.conn.execute("DELETE FROM audit_delete_targets WHERE operation_id=?", [operation_id])
            self.conn.commit()
            journal.unlink()

    async def delete(self, ids=None, *, actor=None, max_seq=None, action="audit.delete", **filters):
        return await run_blocking(self._delete, ids, actor or {}, max_seq, action, filters)

    def _delete(self, ids, actor, max_seq, action, filters):
        with self.lock, self.owner.gate.operation(), self.source_lock:
            self._recover()
            self._sync()
            where, args = self._where(filters, max_seq=max_seq)
            if ids is not None:
                if not ids:
                    return {"deleted": 0, "files": 0}
                where += (" AND " if where else " WHERE ") + "event_id IN (" + ",".join("?" for _ in ids) + ")"
                args.extend(ids)
            operation_id = uuid.uuid4().hex
            actor = {key: value for key, value in actor.items() if key != "user_id"} | {"source": actor.get("source", "system"),
                                                                                     "user_id": actor.get("user_id")}
            marker_id = self._append(build_event(action, actor["user_id"], {"operation_id": operation_id, "phase": "started"},
                                     **{key: value for key, value in actor.items() if key != "user_id"}, result="pending"))
            # Freeze targets before the start marker's sequence so no management event deletes itself.
            self._sync()
            marker_seq = self.conn.execute("SELECT seq FROM audit_events WHERE event_id=?", [marker_id]).fetchone()[0]
            where += (" AND " if where else " WHERE ") + "seq<?"
            args.append(marker_seq)
            count, files = self.conn.execute("SELECT COUNT(*),COUNT(DISTINCT path) FROM audit_events" + where, args).fetchone()
            journal = self.journals / (operation_id + ".jsonl")
            staged = journal.with_suffix(".tmp")
            metadata = {"action": action, "actor": actor, "count": count, "files": files}
            try:
                with staged.open("w", encoding="utf-8") as stream:
                    stream.write(json.dumps(metadata) + "\n")
                    cursor = self.conn.execute("SELECT event_id,path FROM audit_events" + where, args)
                    for row in cursor:
                        stream.write(json.dumps(dict(row)) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(staged, journal)
                with journal.open(encoding="utf-8") as stream:
                    next(stream)
                    for number, raw in enumerate(stream, 1):
                        row = json.loads(raw)
                        self.conn.execute("INSERT INTO audit_delete_targets VALUES(?,?,?)",
                                          [operation_id, row["event_id"], row["path"]])
                        if number % 250 == 0:
                            self.conn.commit()
                self.conn.commit()
                self._rewrite_targets(operation_id)
                self._sync(full=True)
                self._append(build_event(action, actor["user_id"], {
                    "operation_id": operation_id, "phase": "completed", "count": count, "files": files},
                    **{key: value for key, value in actor.items() if key != "user_id"}))
                self.conn.execute("DELETE FROM audit_delete_targets WHERE operation_id=?", [operation_id])
                self.conn.commit()
                journal.unlink()
            finally:
                staged.unlink(missing_ok=True)
            return {"deleted": count, "files": files, "operation_id": operation_id}

    async def retain(self, days, *, at=None):
        if not days:
            return {"deleted": 0, "files": 0}
        at = at or datetime.now(UTC)
        cutoff = (at - timedelta(days=days, microseconds=1)).isoformat()
        page = await self.query(page_size=1, to_time=cutoff)
        if not page["total"]:
            return {"deleted": 0, "files": 0}
        return await self.delete(None, action="audit.clear_auto", to_time=cutoff)
