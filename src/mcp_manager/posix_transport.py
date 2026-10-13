"""POSIX stdio transport whose shutdown always cleans the entire process group."""
import asyncio
import os
import signal
import sys
from contextlib import asynccontextmanager, suppress

import anyio
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from .runtime import GatewayError


class BoundedMessageBuffer:
    def __init__(self, *, max_bytes=8 * 1024 * 1024, encoding="utf-8", errors="strict"):
        import codecs

        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("Message byte budget must be positive")
        self.max_bytes = max_bytes
        self.decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
        self.buffer = bytearray()

    def feed(self, chunk):
        lines = self.decoder.decode(chunk).split("\n")
        result = []
        for index, part in enumerate(lines):
            raw = part.encode("utf-8")
            if len(self.buffer) + len(raw) > self.max_bytes:
                raise GatewayError("message_too_large", "Downstream stdio message exceeds its byte budget")
            self.buffer.extend(raw)
            if index != len(lines) - 1:
                result.append(self.buffer.decode("utf-8"))
                self.buffer.clear()
        return result

    def finish(self):
        tail = self.decoder.decode(b"", final=True)
        if tail or self.buffer:
            raise GatewayError("incomplete_message", "Downstream closed an incomplete JSON message")


def _build_env(user_env):
    env = dict(os.environ)
    for key, value in (user_env or {}).items():
        if "\0" in key or "=" in key:
            raise ValueError(f"invalid env key: {key!r}")
        if value is None:
            env.pop(key, None)
        elif isinstance(value, str):
            env[key] = value
        else:
            env[key] = str(value)
    return env


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
async def posix_stdio(parameters, *, max_message_bytes=8 * 1024 * 1024):
    process = await asyncio.create_subprocess_exec(
        parameters.command, *parameters.args, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=sys.stderr, start_new_session=True,
        cwd=parameters.cwd, env=_build_env(parameters.env))
    to_client, incoming = anyio.create_memory_object_stream(0)
    outgoing, from_client = anyio.create_memory_object_stream(0)

    async def reader():
        buffer = BoundedMessageBuffer(
            max_bytes=max_message_bytes, encoding=parameters.encoding, errors=parameters.encoding_error_handler
        )
        try:
            while chunk := await process.stdout.read(65536):
                for line in buffer.feed(chunk):
                    try:
                        value = await asyncio.to_thread(jsonrpc_message_adapter.validate_json, line, by_name=False)
                        message = SessionMessage(value)
                    except ValueError as exc:
                        message = exc
                    await to_client.send(message)
            buffer.finish()
        except (GatewayError, UnicodeError) as exc:
            await to_client.send(exc)
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
