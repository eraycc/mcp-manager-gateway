"""Deployment definitions use one frozen Python 3.12 runtime and durable readiness."""
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_docker_build_uses_python312_and_never_rewrites_lock():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.12" in dockerfile
    assert "python:3.11" not in dockerfile
    assert "sed -i" not in dockerfile
    assert "uv lock " not in dockerfile
    assert "uv sync --frozen" in dockerfile
    assert "uv export --frozen" in dockerfile


def test_sqlite_compose_has_single_migration_and_ready_healthcheck():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["gateway"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert services["migrate"]["command"] == ["mcp-manager", "upgrade"]
    health = " ".join(services["gateway"]["healthcheck"]["test"])
    assert "/readyz" in health
    assert "mcp-manager-data:/data" in services["gateway"]["volumes"]
    assert "mcp-manager-data:/data" in services["migrate"]["volumes"]


def test_mysql84_compose_persists_database_and_data_dir():
    compose = yaml.safe_load((ROOT / "compose.mysql.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["mysql"]["image"].startswith("mysql:8.4")
    assert services["gateway"]["depends_on"]["mysql"]["condition"] == "service_healthy"
    assert services["gateway"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "mysql-data:/var/lib/mysql" in services["mysql"]["volumes"]
    assert "mcp-manager-data:/data" in services["gateway"]["volumes"]
    assert "DATABASE_URL" in services["gateway"]["environment"]
