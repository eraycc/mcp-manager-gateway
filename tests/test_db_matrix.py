"""The release matrix must reject missing MySQL targets, not hide them as skips."""
import importlib

import pytest
from sqlalchemy import text


def matrix_module():
    import conftest
    return conftest


def test_matrix_never_silently_skips_mysql():
    select_backends = getattr(matrix_module(), "select_backends", lambda values, required: ["sqlite"])
    assert select_backends([], True) == ["sqlite", "mysql80", "mysql84"]
    assert select_backends(["mysql84", "sqlite", "mysql84"], False) == ["mysql84", "sqlite"]


def test_database_target_is_isolated():
    module = importlib.import_module("db_fixture") if importlib.util.find_spec("db_fixture") else None
    validate = getattr(module, "validate_test_url", lambda value: value)
    with pytest.raises(ValueError):
        validate("mysql+asyncmy://root:password@localhost/production")
    assert validate("mysql+asyncmy://root:password@localhost/mmg_test_example").database == "mmg_test_example"


async def test_selected_backend_is_real(dual_db, db_backend):
    async with dual_db.session() as session:
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        actual = dual_db.engine.dialect.name
        assert actual == ("sqlite" if db_backend == "sqlite" else "mysql")
        if actual == "mysql":
            version = (await session.execute(text("SELECT VERSION()"))).scalar_one()
            assert version.startswith("8.0." if db_backend == "mysql80" else "8.4.")
