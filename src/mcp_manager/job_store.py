"""Main-database job statuses, unique result append and bounded reads."""
import asyncio
from datetime import timedelta

from sqlalchemy import delete, func, select

from .catalog_models import ArtifactObject
from .catalog_types import ArtifactRef
from .database import now
from .db_types import strict_json_dumps
from .diagnostics import safe_diagnostic_error
from .job_models import JobRecord, JobResult
from .logs import redact
from .paging import decode_cursor, encode_cursor
from .runtime import GatewayError

TERMINAL = {"completed", "completed_with_errors", "cancelled", "interrupted", "failed"}
STATUSES = TERMINAL | {"queued", "running"}


def status_dict(row):
    return {name: getattr(row, name) for name in
            ("id", "user_id", "action", "status", "total", "completed", "succeeded", "failed",
             "cancelled", "version", "error")} | {
                 "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}


def result_summary(ordinal, result):
    item = redact(result.get("item"))
    label = strict_json_dumps(item)[:256]
    return {"index": ordinal, "item": label, "ok": bool(result.get("ok")),
            "cancelled": bool(result.get("cancelled")), "outcome_unknown": bool(result.get("outcome_unknown")),
            "error": str(result.get("error", ""))[:500]}


class JobStore:
    def __init__(self, db, artifacts, *, signing_key="job-tests"):
        self.db, self.artifacts, self.signing_key = db, artifacts, signing_key

    async def create_job(self, action, total, *, user_id=None, identity=None):
        if not isinstance(action, str) or not 1 <= len(action) <= 128 or type(total) is not int or not 0 <= total <= 1000:
            raise ValueError("Invalid job action or item count")
        async with self.db.locked() as session:
            row = JobRecord(action=action, total=total, user_id=user_id)
            if identity:
                row.id = identity
            session.add(row)
            await session.flush()
            return status_dict(row)

    async def get_status(self, job_id):
        async with self.db.session() as session:
            row = await session.get(JobRecord, job_id)
            return status_dict(row) if row else None

    async def list_jobs(self, *, user_id=None, page=1, limit=50):
        if type(page) is not int or page < 1 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid job page")
        filters = [JobRecord.user_id == user_id] if user_id else []
        async with self.db.session() as session:
            total = await session.scalar(select(func.count()).select_from(JobRecord).where(*filters))
            rows = (await session.scalars(select(JobRecord).where(*filters).order_by(
                JobRecord.created_at.desc(), JobRecord.id).offset((page - 1) * limit).limit(limit))).all()
            return {"items": [status_dict(row) for row in rows], "total": total, "page": page, "page_size": limit}

    async def set_status(self, job_id, status, *, error=""):
        if status not in STATUSES:
            raise ValueError("Invalid job status")
        async with self.db.locked() as session:
            row = await session.get(JobRecord, job_id)
            if not row:
                raise GatewayError("job_not_found", "Job no longer exists")
            if row.status in TERMINAL and row.status != status:
                return status_dict(row)
            row.status, row.error, row.updated_at = status, str(error)[:500], now()
            row.version += 1
            return status_dict(row)

    async def append_result(self, job_id, ordinal, result):
        if type(ordinal) is not int or ordinal < 0 or not isinstance(result, dict):
            raise ValueError("Invalid job result")
        async with self.db.session() as session:
            if await session.get(JobResult, (job_id, ordinal)):
                return False
        # Inputs and errors are diagnostic metadata; successful result bodies are
        # authorized downloadable output and must retain ordinary fields such as "value".
        safe = dict(result)
        safe["item"] = await asyncio.to_thread(redact, result.get("item"))
        if result.get("error"):
            safe["error"] = safe_diagnostic_error(
                ValueError(str(result["error"])), config=result.get("item") if isinstance(result.get("item"), dict) else {}
            )["message"]
        summary = await asyncio.to_thread(result_summary, ordinal, safe)
        ref = await self.artifacts.put_json(safe, max_bytes=8 * 1024 * 1024)
        async with self.db.locked() as session:
            row = await session.get(JobRecord, job_id)
            if not row or ordinal >= row.total:
                raise GatewayError("job_not_found", "Job or result slot no longer exists")
            if await session.get(JobResult, (job_id, ordinal)):
                return False
            if await session.get(ArtifactObject, ref.id) is None:
                session.add(ArtifactObject(id=ref.id, size_bytes=ref.size_bytes, sha256=ref.sha256))
                await session.flush()
            session.add(JobResult(job_id=job_id, ordinal=ordinal, summary=summary,
                                  artifact_id=ref.id, artifact_bytes=ref.size_bytes))
            row.completed += 1
            row.succeeded += int(summary["ok"])
            row.failed += int(not summary["ok"] and not summary["cancelled"])
            row.cancelled += int(summary["cancelled"])
            row.version += 1
            row.updated_at = now()
            return True

    async def list_results(self, job_id, *, cursor=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid result page size")
        binding = "job-results:" + job_id + ":" + str(limit)
        offset = decode_cursor(cursor, binding, self.signing_key)
        async with self.db.session() as session:
            total = await session.scalar(select(func.count()).select_from(JobResult).where(JobResult.job_id == job_id))
            rows = (await session.execute(select(JobResult.ordinal, JobResult.summary).where(
                JobResult.job_id == job_id, JobResult.ordinal >= offset).order_by(JobResult.ordinal).limit(limit + 1))).all()
        selected = rows[:limit]
        following = encode_cursor(selected[-1].ordinal + 1, binding, self.signing_key) if len(rows) > limit else None
        return {"items": [dict(row.summary) for row in selected], "total": total, "next_cursor": following}

    async def get_result_ref(self, job_id, ordinal):
        async with self.db.session() as session:
            row = await session.get(JobResult, (job_id, ordinal))
            return ArtifactRef(row.artifact_id, row.artifact_bytes, row.artifact_id) if row else None

    async def get_result(self, job_id, ordinal):
        ref = await self.get_result_ref(job_id, ordinal)
        return await self.artifacts.read_json(ref, max_bytes=8 * 1024 * 1024) if ref else None

    async def interrupt_unfinished(self):
        while True:
            async with self.db.locked() as session:
                rows = (await session.scalars(select(JobRecord).where(
                    JobRecord.status.in_(("queued", "running"))).limit(250))).all()
                if not rows:
                    return
                for row in rows:
                    row.status, row.error, row.updated_at = "interrupted", "Runtime restarted; operations were not replayed", now()
                    row.version += 1

    async def prune(self, *, at=None, batch_limit=250):
        if type(batch_limit) is not int or not 1 <= batch_limit <= 250:
            raise ValueError("Invalid retention batch")
        cutoff = (at or now()) - timedelta(days=7)
        async with self.db.locked() as session:
            ids = list((await session.scalars(select(JobRecord.id).where(
                JobRecord.status.in_(TERMINAL), JobRecord.updated_at < cutoff).limit(batch_limit))).all())
            if ids:
                await session.execute(delete(JobRecord).where(JobRecord.id.in_(ids)))
            return len(ids)
