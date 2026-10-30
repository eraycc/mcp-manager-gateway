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
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .catalog import SECRET_KEYS
from .derived_index import prepare_index_path
from .log_index_migrations import migrate_call_index
from .log_sources import LogSources


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
        self.root = Path(data_dir) / "logs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.calls_root = self.root / "calls"
        self.calls_root.mkdir(exist_ok=True)
        self.payload_root = self.calls_root / "payloads"
        self.payload_root.mkdir(exist_ok=True)
        index_path = prepare_index_path(data_dir)
        self.sources = LogSources(self.root, data_dir)
        self.timezone = ZoneInfo(timezone_name)
        self.lock = threading.RLock()
        self.capacity = asyncio.Semaphore(256)
        self.export_capacity = asyncio.Semaphore(4)
        self.active = set()
        self.deleted = set()
        self._deletion_offsets = {}
        self._stats_epoch = 0
        self._stats_cache = {}
        tombstones = self.calls_root / ".deletions"
        tombstones.mkdir(exist_ok=True)
        self.tombstones = tombstones
        self._refresh_deleted()
        self.conn = sqlite3.connect(index_path, check_same_thread=False)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.create_function("local_day", 1, lambda stamp: datetime.fromisoformat(stamp).astimezone(self.timezone).date().isoformat())
            self.conn.execute("PRAGMA journal_mode=WAL")
            with self.sources.lock:
                requires_rebuild = migrate_call_index(self.conn)
            self._rebuild(full=requires_rebuild)
            from .audit_store import AuditStore

            self.audits = AuditStore(self)
        except BaseException:
            self.conn.close()
            self.sources.close()
            raise

    def _refresh_deleted(self):
        """Consume durable deletion decisions before every writer mutates a source."""
        paths = [*(self.root / "deletions").glob("????-??-??.jsonl"),
                 *self.tombstones.glob("????-??-??.jsonl")]
        for path in paths:
            stat = path.stat()
            inode, offset = self._deletion_offsets.get(path, (None, 0))
            if inode != stat.st_ino or offset > stat.st_size:
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
            self._deletion_offsets[path] = (stat.st_ino, offset)

    async def append(self, event):
        async with self.capacity:
            return await asyncio.to_thread(self._append, event)

    def _append(self, event):
        event = redact(dict(event))
        event.setdefault("id", uuid.uuid4().hex)
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
        separated = log_summary_and_ref(event)
        payload_ref = None
        if separated["payload"] is not None:
            payload_raw = json.dumps(
                separated["payload"],
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            ).encode("utf-8")
            payload_ref = self._write_payload(payload_raw)
            event = separated["summary"] | {"_payload_ref": payload_ref}
        raw = (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self.lock, self.sources.lock:
            self._refresh_deleted()
            if event["id"] in self.deleted:
                return event["id"]
            if event.get("status") == "running":
                self.active.add(event["id"])
            else:
                self.active.discard(event["id"])
            with path.open("a+b") as f:
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
            self._index(event, path, offset, len(raw), source_generation=self.sources.generation(path))
            name = self.sources.name(path)
            self._set_offset(name, offset + len(raw))
            self.conn.execute("INSERT OR REPLACE INTO call_source_versions VALUES(?,?)",
                              [name, self.sources.generation(path)])
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
        with self.lock, self.sources.lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                return {"id": identity, "size": len(raw)}
            temporary = self.payload_root / ("." + uuid.uuid4().hex + ".tmp")
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

    def _index(self, event, path, offset, size, *, source_generation=None):
        if source_generation is not None and source_generation != self.sources.generation(path):
            return False
        if event["id"] in self.deleted:
            return False
        fields = ("timestamp", "user_id", "username", "token_id", "token_name", "mcp_id",
                  "mcp_name", "tool_name", "status", "source", "duration_ms")
        self.conn.execute("INSERT OR REPLACE INTO calls VALUES(" + ",".join("?" * 15) + ")",
                          [event["id"], self.sources.name(path), offset, size] + [
                              self.normalize_time(event["timestamp"], default_timezone=UTC)
                              if f == "timestamp" else event.get(f) for f in fields])
        payload = event.get("_payload_ref")
        if isinstance(payload, dict):
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

    def _rebuild(self, *, full=False):
        with self.lock, self.sources.lock:
            self._refresh_deleted()
            if full:
                self.conn.execute("DELETE FROM calls")
                self.conn.execute("DELETE FROM index_offsets")
                self.conn.execute("DELETE FROM payloads")
                self.conn.execute("DELETE FROM call_owners")
                self.conn.execute("DELETE FROM call_source_versions")
            paths = self.sources.paths()
            names = {self.sources.name(path) for path in paths}
            indexed_names = {
                row[0] for row in self.conn.execute("SELECT path FROM index_offsets")
            }
            removed = indexed_names - names
            for name in removed:
                self.conn.execute("DELETE FROM calls WHERE path=?", [name])
                self.conn.execute("DELETE FROM index_offsets WHERE path=?", [name])
            if self.deleted:
                self.conn.execute(
                    "DELETE FROM calls WHERE id IN (" + ",".join("?" for _ in self.deleted) + ")",
                    list(self.deleted),
                )
            for path in paths:
                name = self.sources.name(path)
                generation = self.sources.generation(path)
                saved = self.conn.execute(
                    "SELECT offset,generation FROM index_offsets LEFT JOIN call_source_versions USING(path) WHERE path=?", [name]
                ).fetchone()
                start = saved["offset"] if saved else 0
                size = path.stat().st_size
                if not saved or start > size or saved["generation"] != generation:
                    self.conn.execute("DELETE FROM calls WHERE path=?", [name])
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
                                continue
                            self.normalize_time(event["timestamp"], default_timezone=UTC)
                        except (ValueError, KeyError, TypeError):
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
        await asyncio.to_thread(self._rebuild, full=True)

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
        return await asyncio.to_thread(self._query, page, page_size, filters)

    def _query(self, page, page_size, filters):
        where, values = self._where(filters)
        page_size = max(1, min(int(page_size), 200))
        with self.lock, self.sources.lock:
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
        return await asyncio.to_thread(self._reference, id, user_id)

    def _reference(self, id, user_id):
        with self.lock, self.sources.lock:
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
            }

    async def preview(self, id, *, user_id=None, max_bytes=65536):
        reference = await self.reference(id, user_id=user_id)
        if not reference:
            return None
        def read():
            with reference["path"].open("rb") as source:
                source.seek(reference["offset"])
                raw = source.read(min(reference["size"], max_bytes))
            return raw
        raw = await asyncio.to_thread(read)
        truncated = reference["size"] > max_bytes
        return {
            "preview": raw[:max_bytes].decode("utf-8", errors="ignore"),
            "truncated": truncated,
            "size_bytes": reference["size"],
        }

    async def download_chunks(self, reference, *, chunk_size=65536):
        source = await asyncio.to_thread(reference["path"].open, "rb")
        remaining = reference["size"]
        try:
            await asyncio.to_thread(source.seek, reference["offset"])
            while remaining:
                chunk = await asyncio.to_thread(source.read, min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(source.close)

    async def detail(self, id, *, user_id=None):
        return await asyncio.to_thread(self._detail, id, user_id)

    def _detail(self, id, user_id):
        with self.lock, self.sources.lock:
            row = self.conn.execute("SELECT * FROM calls WHERE id=?", [id]).fetchone()
            if not row or user_id is not None and row["user_id"] != user_id:
                return None
            payload = self.conn.execute(
                "SELECT path,size FROM payloads WHERE log_id=?",
                [id],
            ).fetchone()
            if payload:
                event = self._read_payload({
                    "path": self._payload_reference_path(payload["path"]),
                    "size": payload["size"],
                })
            else:
                with (self.root / row["path"]).open("rb") as source:
                    source.seek(row["offset"])
                    event = json.loads(source.read(row["size"]))
            if event.get("id") != id or (user_id is not None and event.get("user_id") != user_id):
                raise RuntimeError("Log index does not match source; rebuild required")
            event["status"] = row["status"]
            return event

    async def export_batch(self, filters, *, cursor=None, limit=100):
        return await asyncio.to_thread(self._export_batch, filters, cursor, min(200, max(1, limit)))

    def _export_batch(self, filters, cursor, limit):
        where, values = self._where(filters)
        if cursor:
            where += (" AND " if where else " WHERE ") + "(timestamp < ? OR (timestamp = ? AND id < ?))"
            values += [cursor[0], cursor[0], cursor[1]]
        with self.lock, self.sources.lock:
            rows = self.conn.execute("SELECT * FROM calls" + where +
                " ORDER BY timestamp DESC, id DESC LIMIT ?", values + [limit]).fetchall()
            result = []
            payload_rows = {
                item["log_id"]: item
                for item in self.conn.execute(
                    "SELECT log_id,path,size FROM payloads WHERE log_id IN ("
                    + ",".join("?" for _ in rows) + ")",
                    [row["id"] for row in rows],
                )
            } if rows else {}
            for row in rows:
                payload = payload_rows.get(row["id"])
                if payload:
                    event = self._read_payload({
                        "path": self._payload_reference_path(payload["path"]),
                        "size": payload["size"],
                    })
                else:
                    with (self.root / row["path"]).open("rb") as source:
                        source.seek(row["offset"])
                        event = json.loads(source.read(row["size"]))
                if event.get("id") != row["id"]:
                    raise RuntimeError("Log index does not match source; rebuild required")
                event["status"] = row["status"]
                result.append(event)
            next_cursor = (rows[-1]["timestamp"], rows[-1]["id"]) if rows else None
            return result, next_cursor

    async def delete(self, ids=None, **filters):
        return await asyncio.to_thread(self._delete, ids, filters)

    def _delete(self, ids, filters):
        with self.lock, self.sources.lock:
            where, args = self._where(filters)
            selected = {r[0] for r in self.conn.execute("SELECT id FROM calls" + where, args)}
            if ids is not None:
                selected.intersection_update(ids)
            if not selected:
                for path in self.sources.paths():
                    if path.stat().st_size == 0:
                        path.unlink()
                return 0
            journal = self.tombstones / (datetime.now(self.timezone).strftime("%Y-%m-%d") + ".jsonl")
            with journal.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ids": sorted(selected)}) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.deleted.update(selected)
            self.active.difference_update(selected)
            payloads = list(self.conn.execute(
                "SELECT artifact_id,path FROM payloads WHERE log_id IN ("
                + ",".join("?" for _ in selected) + ")",
                list(selected),
            ))
            try:
                self._rewrite_deleted(selected)
            finally:
                self._rebuild(full=True)
            for payload in payloads:
                remaining = self.conn.execute(
                    "SELECT 1 FROM payloads WHERE artifact_id=? LIMIT 1",
                    [payload["artifact_id"]],
                ).fetchone()
                if remaining is None:
                    (self._payload_reference_path(payload["path"])).unlink(missing_ok=True)
            return len(selected)

    def _rewrite_deleted(self, selected):
            for path in self.sources.paths():
                temp = path.with_suffix(".rewrite")
                try:
                    with path.open("rb") as source, temp.open("wb") as target:
                        for raw in source:
                            try:
                                keep = json.loads(raw).get("id") not in selected
                            except (ValueError, TypeError):
                                keep = True
                            if keep:
                                target.write(raw)
                        target.flush()
                        os.fsync(target.fileno())
                    if temp.stat().st_size:
                        os.replace(temp, path)
                    else:
                        path.unlink()
                    self.sources.bump(path)
                finally:
                    temp.unlink(missing_ok=True)

    async def stats(self, *, user_id=None, token_id=None):
        return await asyncio.to_thread(self._stats, user_id, token_id)

    def _stats(self, user_id, token_id):
        cache_key = (user_id, token_id)
        current = time.monotonic()
        with self.lock, self.sources.lock:
            cached = self._stats_cache.get(cache_key)
            if cached and cached[0] > current and cached[1] == self._stats_epoch:
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
            self._stats_cache[cache_key] = (current + 10.0, self._stats_epoch, result)
            return copy.deepcopy(result)

    async def token_counts(self, ids):
        def query():
            if not ids:
                return {}
            with self.lock, self.sources.lock:
                rows = self.conn.execute(
                    "SELECT token_id,count(*) call_count,sum(status='success') success_count,"
                    "sum(status IN ('tool_error','gateway_error','auth_rejected')) failed_count FROM calls "
                    "WHERE source='gateway' AND status!='running' AND token_id IN (" +
                    ",".join("?" for _ in ids) + ") GROUP BY token_id", ids)
                return {row["token_id"]: {k: row[k] for k in ("call_count", "success_count", "failed_count")}
                        for row in rows}
        return await asyncio.to_thread(query)

    async def audit(self, action, user_id, details, **context):
        return await self.audits.append(action, user_id, details, **context)

    async def close(self):
        with self.lock, self.sources.lock:
            self.conn.close()
            self.sources.close()
