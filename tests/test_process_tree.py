import asyncio
import ctypes
import json
import os
import sys
from pathlib import Path

from mcp_manager.runtime import Runtime, ServerSpec
from mcp_manager.transports import connect


def alive(pid):
    if sys.platform == "win32":
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path("/proc") / str(pid) / "stat"
        if stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z "):
            return False
        return True
    except ProcessLookupError:
        return False


async def test_last_lease_closes_child_process_tree():
    runtime = Runtime(connect)
    lease = runtime.create_lease("u", "t")
    fixture = str(Path(__file__).parent / "fixtures/process_tree_server.py")
    spec = ServerSpec("tree", "stdio", {"command": sys.executable, "args": [fixture]})
    try:
        result = await runtime.call(spec, lease.id, "process_identity", {})
        assert not result.get("isError"), result
        pids = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        assert alive(pids["parent_pid"]) and alive(pids["child_pid"])
        await runtime.release(lease.id)
        for _ in range(100):
            if not alive(pids["parent_pid"]) and not alive(pids["child_pid"]):
                break
            await asyncio.sleep(.05)
        assert not alive(pids["parent_pid"])
        assert not alive(pids["child_pid"])
    finally:
        await runtime.close()
