"""Public project metadata, shared by MCP servers, CLI and console."""
import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

DEFAULT_NAME = "mcp-manager-gateway"
DEFAULT_VERSION = "1.0.0"
SOURCE_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def load_project_identity(pyproject_path=None):
    try:
        path = Path(pyproject_path) if pyproject_path is not None else SOURCE_PYPROJECT
        source_exists = path.is_file()
    except (OSError, TypeError, ValueError):
        source_exists = False
    if source_exists:
        try:
            project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
            name, project_version = project["name"].strip(), project["version"].strip()
            if name and project_version:
                return name, project_version
        except (AttributeError, KeyError, OSError, TypeError, UnicodeError, tomllib.TOMLDecodeError):
            return DEFAULT_NAME, DEFAULT_VERSION
    try:
        package = distribution(DEFAULT_NAME)
        name = (package.metadata.get("Name") or "").strip()
        project_version = package.version.strip()
        if name and project_version:
            return name, project_version
    except (AttributeError, OSError, PackageNotFoundError, TypeError):
        pass
    return DEFAULT_NAME, DEFAULT_VERSION


NAME, VERSION = load_project_identity()
PROJECT = {
    "name": NAME,
    "version": VERSION,
    "project_url": "https://github.com/eraycc/mcp-manager-gateway",
    "releases_url": "https://github.com/eraycc/mcp-manager-gateway/releases",
    "pypi_url": "https://pypi.org/project/mcp-manager-gateway/",
    "issues_url": "https://github.com/eraycc/mcp-manager-gateway/issues",
    "author": "eraycc",
    "author_url": "https://github.com/eraycc",
}
