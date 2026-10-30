"""Explicit administrator cleanup, bounded batches and exact managed backup identity."""
import os
import re
from datetime import UTC, datetime

from fastapi import HTTPException

from .database import SystemSetting
from .storage_async import run_blocking
from .storage_migration import LAYOUT_KEY


async def current_backup(state):
    async with state.db.session() as session:
        row = await session.get(SystemSetting, LAYOUT_KEY)
        receipt = dict(row.value) if row else {}
    identity = receipt.get("run_id")
    if not isinstance(identity, str) or not re.fullmatch("[0-9a-f]{32}", identity):
        return None
    root = state.config.data_dir.resolve()
    folder = root / "backups/storage" / identity
    def inspect():
        if folder.is_symlink() or folder.is_junction() or not folder.resolve().is_relative_to(root):
            raise HTTPException(409, "Backup directory is not a regular managed directory")
        return folder.is_dir()
    return {"id": identity, "exists": await run_blocking(inspect),
            "ready": receipt.get("phase") == "completed", "status": receipt.get("backup_delete_status", "retained"), "deleted_at": receipt.get("backup_deleted_at")}


def _backup_paths(folder):
    def visit(path, depth):
        if depth > 32 or path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(folder):
            raise RuntimeError("Backup contains unmanaged paths; inspect it manually")
        if path.is_dir():
            with os.scandir(path) as entries:
                for entry in entries:
                    yield from visit(path / entry.name, depth + 1)
            yield path, True
        elif path.is_file():
            yield path, False
        else:
            raise RuntimeError("Backup contains an unsupported file")
    yield from visit(folder, 0)


async def delete_backup(state, identity, authorize, user_id):
    backup = await current_backup(state)
    if not backup or backup["id"] != identity or not backup["ready"]:
        raise HTTPException(409, "Only the completed migration backup can be deleted")
    folder = (state.config.data_dir.resolve() / "backups/storage" / identity)
    deleted = 0
    async with state.db.locked() as session:
        await authorize(session)
        row = await session.get(SystemSetting, LAYOUT_KEY)
        if not row or row.value.get("run_id") != identity or row.value.get("phase") != "completed":
            raise HTTPException(409, "Migration state changed during cleanup")
        row.value = dict(row.value, backup_delete_status="deleting")
    if backup["exists"]:
        cursor = _backup_paths(folder)
        def remove_batch():
            count = 0
            for _ in range(100):
                item = next(cursor, None)
                if item is None:
                    return count, True
                path, directory = item
                # Revalidate after discovery, before each exact deletion.
                if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(folder):
                    raise RuntimeError("Backup path changed during cleanup")
                if directory:
                    path.rmdir()
                else:
                    path.unlink()
                    count += 1
            return count, False
        try:
            while True:
                async with state.db.locked() as session:
                    await authorize(session)
                    row = await session.get(SystemSetting, LAYOUT_KEY)
                    if not row or row.value.get("run_id") != identity or row.value.get("phase") != "completed":
                        raise HTTPException(409, "Migration state changed during cleanup")
                    count, complete = await run_blocking(remove_batch)
                deleted += count
                if complete:
                    break
        finally:
            await run_blocking(cursor.close)
    async with state.db.locked() as session:
        await authorize(session)
        row = await session.get(SystemSetting, LAYOUT_KEY)
        if row and row.value.get("run_id") == identity:
            row.value = dict(row.value, backup_delete_status="deleted", backup_deleted_at=datetime.now(UTC).isoformat())
    await state.logs.audit("storage.backup.delete", user_id, {"backup_id": identity, "files": deleted})
    return {"deleted_files": deleted, "backup_id": identity}


async def clear_query_caches(state, authorize, user_id):
    deleted = 0
    for cache in (state.catalog.cache_store, state.embeddings.store):
        while True:
            async with state.db.locked() as session:
                await authorize(session)
                def clear_batch(cache=cache):
                    with cache.gate.operation(), cache.lock:
                        result = cache.connection.execute("""DELETE FROM cache_items WHERE domain=?
                            AND key IN(SELECT key FROM cache_items WHERE domain=? LIMIT 250)""",
                            [cache.domain, cache.domain])
                        return result.rowcount
                count = await run_blocking(clear_batch)
            deleted += count
            if count < 250:
                break
    await state.logs.audit("storage.query_cache.clear", user_id, {"deleted": deleted})
    return {"deleted": deleted}
