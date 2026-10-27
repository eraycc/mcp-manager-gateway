"""Durable audit JSONL with a rebuildable index and recoverable physical deletion."""
import asyncio
import hashlib
import json
import logging
import math
import os
import uuid
from datetime import UTC, datetime, timedelta

from .audit_events import build_event, normalize, timestamp
from .log_index_migrations import migrate_audit_index

LOGGER = logging.getLogger(__name__)
COLUMNS = ("event_id", "timestamp", "action", "category", "actor_type", "user_id", "username",
           "target_type", "target_id", "target_name", "result", "source", "ip", "request_id")
FILTERS = {"action", "category", "user_id", "username", "actor_type", "target_type", "target_id",
           "result", "source", "ip", "q", "from_time", "to_time"}
MAX_LINE = 1024 * 1024


class AuditStore:
    def __init__(self, owner):
        self.owner, self.conn, self.lock = owner, owner.conn, owner.lock
        self.root = owner.root / "audit"
        self.root.mkdir(exist_ok=True)
        self.journals = self.root / ".deletions"
        self.journals.mkdir(exist_ok=True)
        self.write_failures = self.index_failures = 0
        self.last_error = None
        self.skipped = {}
        with self.lock:
            migrate_audit_index(self.conn)
            self._recover()
            self._sync()

    def _error(self, exc, *, projection=False):
        if projection:
            self.index_failures += 1
        else:
            self.write_failures += 1
        self.last_error = {"type": type(exc).__name__, "timestamp": timestamp(), "projection": projection}
        LOGGER.error("Audit %s failed (%s)", "projection" if projection else "persistence", type(exc).__name__)

    async def append(self, action, user_id, details, *, require=False, **context):
        async with self.owner.capacity:
            try:
                event = build_event(action, user_id, details, **context)
                return await asyncio.to_thread(self._append, event)
            except Exception as exc:
                self._error(exc)
                if require:
                    raise
                return None

    def _write(self, event):
        stamp = datetime.fromisoformat(event["timestamp"])
        path = self.root / stamp.astimezone(self.owner.timezone).strftime("%Y-%m-%d.jsonl")
        raw = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with path.open("a+b") as stream:
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
        with self.lock:
            identity = self._write(event)
            try:
                self._sync()
            except Exception as exc:  # noqa: BLE001 -- durable JSONL is recovered on the next index pass.
                self.conn.rollback()
                self._error(exc, projection=True)
            return identity

    def _event(self, raw, path, offset):
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError("Audit event must be an object")
        digest = hashlib.sha256(path.name.encode() + str(offset).encode() + raw).hexdigest()
        return normalize(value, fallback_id=digest)

    def _index(self, event, path, offset, size, scan_id):
        summary = json.dumps(event["details"], ensure_ascii=False, separators=(",", ":"))[:300]
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
            saved = self.conn.execute("SELECT offset,mtime_ns FROM audit_index_offsets WHERE path=?", [path.name]).fetchone()
            rescan = full or saved is None or saved["offset"] > info.st_size or (
                saved["offset"] == info.st_size and saved["mtime_ns"] != info.st_mtime_ns)
            start = 0 if rescan else saved["offset"]
            if not rescan and start == info.st_size:
                continue
            scan_id = uuid.uuid4().hex
            skipped = 0 if rescan else self.skipped.get(path.name, 0)
            committed = start
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
                    except (ValueError, KeyError, TypeError):
                        skipped += 1
            if rescan:
                self.conn.execute("DELETE FROM audit_events WHERE path=? AND scan_id!=?", [path.name, scan_id])
            self.conn.execute("INSERT INTO audit_index_offsets VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET "
                              "offset=excluded.offset,mtime_ns=excluded.mtime_ns",
                              [path.name, committed, info.st_mtime_ns])
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
        with self.lock:
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
        return {key: row[key] for key in (*COLUMNS, "summary")}

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
            with self.lock:
                self._sync()
                return self._detail(self.conn.execute("SELECT * FROM audit_events WHERE event_id=?", [identity]).fetchone())
        return await asyncio.to_thread(read)

    async def watermark(self):
        def read():
            with self.lock:
                self._sync()
                return self.conn.execute("SELECT COALESCE(MAX(seq),0) FROM audit_events").fetchone()[0]
        return await asyncio.to_thread(read)

    async def export_batch(self, *, max_seq, cursor=None, limit=200, **filters):
        def read():
            with self.lock:
                self._sync()
                where, args = self._where(filters, max_seq=max_seq, cursor=cursor)
                rows = self.conn.execute("SELECT * FROM audit_events" + where +
                    " ORDER BY timestamp DESC,event_id DESC LIMIT ?", args + [limit]).fetchall()
                return [self._detail(row) for row in rows], ((rows[-1]["timestamp"], rows[-1]["event_id"]) if rows else None)
        return await asyncio.to_thread(read)

    async def status(self):
        def read():
            with self.lock:
                self._sync()
                row = self.conn.execute("SELECT COUNT(*),MIN(timestamp),MAX(timestamp) FROM audit_events").fetchone()
                files = list(self.root.glob("????-??-??.jsonl"))
                return {"events": row[0], "oldest": row[1], "newest": row[2], "files": len(files),
                        "bytes": sum(path.stat().st_size for path in files), "write_failures": self.write_failures,
                        "index_failures": self.index_failures, "last_error": self.last_error,
                        "skipped_records": sum(self.skipped.values()),
                        "pending_deletions": len(list(self.journals.glob("*.jsonl")))}
        return await asyncio.to_thread(read)

    async def rebuild(self):
        def run():
            with self.lock:
                self._recover()
                self._sync(full=True)
        await asyncio.to_thread(run)

    def _rewrite_targets(self, operation_id):
        paths = self.conn.execute("SELECT DISTINCT path FROM audit_delete_targets WHERE operation_id=?", [operation_id]).fetchall()
        for row in paths:
            path = self.root / row[0]
            if not path.exists():
                continue
            temporary = path.with_name("." + path.name + "." + operation_id + ".tmp")
            try:
                with path.open("rb") as source, temporary.open("wb") as target:
                    while True:
                        offset = source.tell()
                        raw = source.readline()
                        if not raw:
                            break
                        try:
                            identity = self._event(raw, path, offset)["event_id"]
                        except (ValueError, KeyError, TypeError):
                            identity = None
                        deleted = identity and self.conn.execute("SELECT 1 FROM audit_delete_targets WHERE "
                            "operation_id=? AND event_id=?", [operation_id, identity]).fetchone()
                        if not deleted:
                            target.write(raw)
                    target.flush()
                    os.fsync(target.fileno())
                if temporary.stat().st_size:
                    os.replace(temporary, path)
                else:
                    path.unlink()
                self.conn.execute("DELETE FROM audit_index_offsets WHERE path=?", [path.name])
                self.conn.execute("DELETE FROM audit_events WHERE path=? AND event_id IN "
                    "(SELECT event_id FROM audit_delete_targets WHERE operation_id=?)", [path.name, operation_id])
            finally:
                temporary.unlink(missing_ok=True)

    def _recover(self):
        for journal in sorted(self.journals.glob("*.jsonl")):
            operation_id = journal.stem
            with journal.open(encoding="utf-8") as stream:
                metadata = json.loads(next(stream))
                for raw in stream:
                    target = json.loads(raw)
                    # Journal paths are generated locally and must remain direct day files.
                    if len(target["path"]) != 16 or "/" in target["path"] or "\\" in target["path"]:
                        raise ValueError("Invalid audit deletion journal")
                    self.conn.execute("INSERT OR IGNORE INTO audit_delete_targets VALUES(?,?,?)",
                                      [operation_id, target["event_id"], target["path"]])
            self._rewrite_targets(operation_id)
            self._sync(full=True)
            self._append(build_event(metadata["action"], metadata["actor"].get("user_id"), {
                "operation_id": operation_id, "phase": "recovered", "count": metadata["count"],
                "files": metadata["files"]}, **{key: value for key, value in metadata["actor"].items() if key != "user_id"}))
            self.conn.execute("DELETE FROM audit_delete_targets WHERE operation_id=?", [operation_id])
            self.conn.commit()
            journal.unlink()

    async def delete(self, ids=None, *, actor=None, max_seq=None, action="audit.delete", **filters):
        return await asyncio.to_thread(self._delete, ids, actor or {}, max_seq, action, filters)

    def _delete(self, ids, actor, max_seq, action, filters):
        with self.lock:
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
            self._append(build_event(action, actor["user_id"], {"operation_id": operation_id, "phase": "started"},
                                     **{key: value for key, value in actor.items() if key != "user_id"}, result="pending"))
            # Freeze targets before the start marker's sequence so no management event deletes itself.
            marker_seq = self.conn.execute("SELECT MAX(seq) FROM audit_events").fetchone()[0]
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
                    for raw in stream:
                        row = json.loads(raw)
                        self.conn.execute("INSERT INTO audit_delete_targets VALUES(?,?,?)",
                                          [operation_id, row["event_id"], row["path"]])
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
