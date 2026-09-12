"""Public project metadata, shared by CLI and the authenticated console."""
from importlib.metadata import PackageNotFoundError, version

try:
    VERSION = version("mcp-manager-gateway")
except PackageNotFoundError:
    VERSION = "0.1.0"

PROJECT = {
    "name": "MCP Manager Gateway",
    "version": VERSION,
    "project_url": "https://github.com/eraycc/mcp-manager-gateway",
    "releases_url": "https://github.com/eraycc/mcp-manager-gateway/release",
    "issues_url": "https://github.com/eraycc/mcp-manager-gateway/issues",
    "author": "eraycc",
    "author_url": "https://github.com/eraycc",
}
