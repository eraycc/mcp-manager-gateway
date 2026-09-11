"""Bounded background operations with durable, non-replayed progress snapshots."""
import asyncio
import json
import logging
from pathlib import Path
from uuid import uuid4

from .logs import redact


class Jobs:
    def __init__(self, data_dir=None):
        self.items = {}
        self.tasks = {}
        self.limit = asyncio.Semaphore(4)
        self.directory = Path(data_dir) / "jobs" if data_dir else None
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
            for path in self.directory.glob("*.json"):
                try:
                    job = json.loads(path.read_text(encoding="utf-8"))
                    if job["status"] in {"queued", "running"}:
                        job.update(status="interrupted", error="Runtime restarted; operations were not replayed")
                    self.items[job["id"]] = job
                except (OSError, ValueError, KeyError):
                    continue

    def persist(self, job):
        if not self.directory:
            return
        try:
            path = self.directory / (job["id"] + ".json")
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(redact(job), ensure_ascii=False, default=str), encoding="utf-8")
            temp.replace(path)
        except OSError:
            logging.getLogger(__name__).exception("Job snapshot write failed")

    def submit(self, action, items, operation, user_id=None):
        job = {"id": str(uuid4()), "action": action, "status": "queued", "total": len(items),
               "completed": 0, "results": [], "user_id": user_id}
        self.items[job["id"]] = job
        self.persist(job)

        async def run():
            job["status"] = "running"
            self.persist(job)
            async def one(item):
                async with self.limit:
                    try:
                        result = await operation(item)
                        job["results"].append({"item": item, "ok": True, "result": result})
                    except Exception as exc:
                        job["results"].append({"item": item, "ok": False, "error": str(exc)})
                    finally:
                        job["completed"] += 1
                        self.persist(job)
            try:
                await asyncio.gather(*(one(item) for item in items))
                job["status"] = "completed" if all(x["ok"] for x in job["results"]) else "completed_with_errors"
            except asyncio.CancelledError:
                job["status"] = "cancelled"
            finally:
                self.persist(job)
        self.tasks[job["id"]] = asyncio.create_task(run())
        return job

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
