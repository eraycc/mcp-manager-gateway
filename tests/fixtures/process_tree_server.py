import os
import subprocess
import sys

from mcp.server import MCPServer

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
server = MCPServer("process-tree-fixture")


@server.tool()
def process_identity() -> dict:
    return {"parent_pid": os.getpid(), "child_pid": child.pid}


if __name__ == "__main__":
    server.run(transport="stdio")
