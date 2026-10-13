"""Shared release-contract fixtures."""

import pytest


def select_backends(values, required=False):
    requested = list(dict.fromkeys(values or (["sqlite", "mysql80", "mysql84"] if required else ["sqlite"])))
    if required and set(requested) != {"sqlite", "mysql80", "mysql84"}:
        raise ValueError("--require-mysql requires sqlite, mysql80 and mysql84")
    return requested


@pytest.fixture
def api_paths(db_app):
    """Return the concrete OpenAPI surface from the running candidate."""
    return db_app.app.openapi()["paths"]
