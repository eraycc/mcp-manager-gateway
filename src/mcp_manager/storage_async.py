"""Join blocking storage work before propagating cancellation to its owner."""
import asyncio


async def join_task(task):
    """Wait through repeated cancellation; the caller retains cancellation ownership."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def run_blocking(function, /, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await join_task(task)
        except (Exception, asyncio.CancelledError):  # noqa: BLE001,S110 - preserve caller cancellation after joining
            pass
        raise
