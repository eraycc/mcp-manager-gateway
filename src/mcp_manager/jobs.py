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

        recorded = set()

        def record(index, result):
            if index in recorded:
                return
            recorded.add(index)
            job["results"].append(result)
            job["completed"] = len(job["results"])
            self.persist(job)

        def cancelled(index, item):
            record(index, {"item": item, "ok": False, "cancelled": True, "error": "Operation cancelled"})

        def finish_cancelled():
            # Tasks cancelled before their coroutine starts cannot record an outcome.
            for index, item in enumerate(items):
                if index not in recorded:
                    cancelled(index, item)
            job["status"] = "cancelled"
            self.persist(job)

        async def run():
            job["status"] = "running"
            self.persist(job)

            async def one(index, item):
                try:
                    async with self.limit:
                        result = await operation(item)
                    record(index, {"item": item, "ok": True, "result": result})
                except asyncio.CancelledError:
                    cancelled(index, item)
                    raise
                except Exception as exc:
                    record(index, {"item": item, "ok": False, "error": str(exc)})

            children = [asyncio.create_task(one(index, item)) for index, item in enumerate(items)]
            try:
                await asyncio.gather(*children)
                job["status"] = "completed" if all(x["ok"] for x in job["results"]) else "completed_with_errors"
            except asyncio.CancelledError:
                # gather does not cancel siblings when just one child is cancelled.
                for child in children:
                    if not child.done():
                        child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
                finish_cancelled()
            finally:
                self.persist(job)

        def done(task):
            self.tasks.pop(job["id"], None)
            if task.cancelled():
                finish_cancelled()

        task = asyncio.create_task(run())
        self.tasks[job["id"]] = task
        task.add_done_callback(done)
        return job

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
