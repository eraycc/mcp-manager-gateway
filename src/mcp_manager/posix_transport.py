"""POSIX stdio transport whose shutdown always cleans the entire process group."""
import asyncio
import os
import signal
import sys
from contextlib import asynccontextmanager, suppress

import anyio
from mcp.client.stdio import get_default_environment
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


async def close_group(process):
    if process.stdin:
        process.stdin.close()
    # A descendant may keep stdout open, so process.wait() alone is not an exit probe.
    deadline = asyncio.get_running_loop().time() + 2
    while process.returncode is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(.01)
    # Always clean the group, including when its original leader exited gracefully.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + 2
    while group_alive(process.pid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(.01)
    if group_alive(process.pid):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), 2)


@asynccontextmanager
async def posix_stdio(parameters):
    process = await asyncio.create_subprocess_exec(
        parameters.command, *parameters.args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=sys.stderr, start_new_session=True,
        cwd=parameters.cwd, env=get_default_environment() | (parameters.env or {}))
    to_client, incoming = anyio.create_memory_object_stream(0)
    outgoing, from_client = anyio.create_memory_object_stream(0)

    async def reader():
        buffer = ""
        import codecs
        decoder = codecs.getincrementaldecoder(parameters.encoding)(errors=parameters.encoding_error_handler)
        try:
            while chunk := await process.stdout.read(65536):
                buffer += decoder.decode(chunk)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    try:
                        message = SessionMessage(jsonrpc_message_adapter.validate_json(line, by_name=False))
                    except ValueError as exc:
                        message = exc
                    await to_client.send(message)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, ConnectionError):
            pass
        finally:
            await to_client.aclose()

    async def writer():
        try:
            async for message in from_client:
                value = message.message.model_dump_json(by_alias=True, exclude_unset=True) + "\n"
                process.stdin.write(value.encode(parameters.encoding, errors=parameters.encoding_error_handler))
                await process.stdin.drain()
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, ConnectionError):
            await to_client.aclose()

    tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
    try:
        yield incoming, outgoing
    finally:
        # An independent task completes cleanup even when the MCP owner was cancelled.
        cleanup = asyncio.create_task(close_group(process))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await incoming.aclose()
            await outgoing.aclose()
            await to_client.aclose()
            await from_client.aclose()
