import pytest

from mcp_manager import posix_transport


def test_build_env_inherits_arbitrary_upstream_variable(monkeypatch):
    monkeypatch.setenv("MMG_UPSTREAM_ONLY", "from-parent")

    result = posix_transport._build_env({})

    assert result["MMG_UPSTREAM_ONLY"] == "from-parent"


def test_build_env_user_value_overrides_upstream(monkeypatch):
    monkeypatch.setenv("MMG_OVERRIDE", "from-parent")

    result = posix_transport._build_env({"MMG_OVERRIDE": "from-user"})

    assert result["MMG_OVERRIDE"] == "from-user"


def test_build_env_none_removes_upstream_variable(monkeypatch):
    monkeypatch.setenv("MMG_REMOVED", "from-parent")

    result = posix_transport._build_env({"MMG_REMOVED": None})

    assert "MMG_REMOVED" not in result


def test_build_env_preserves_explicit_empty_string(monkeypatch):
    monkeypatch.setenv("MMG_EMPTY", "from-parent")

    result = posix_transport._build_env({"MMG_EMPTY": ""})

    assert "MMG_EMPTY" in result
    assert result["MMG_EMPTY"] == ""


def test_build_env_converts_non_string_value():
    result = posix_transport._build_env({"MMG_NUMBER": 7})

    assert result["MMG_NUMBER"] == "7"


@pytest.mark.parametrize("key", ["BAD=KEY", "BAD\0KEY"])
def test_build_env_rejects_invalid_posix_key(key):
    with pytest.raises(ValueError, match="invalid env key"):
        posix_transport._build_env({key: "value"})


def test_build_env_takes_a_fresh_snapshot_on_each_call(monkeypatch):
    monkeypatch.setenv("MMG_SNAPSHOT", "first")
    first = posix_transport._build_env({})
    monkeypatch.setenv("MMG_SNAPSHOT", "second")

    second = posix_transport._build_env({})

    assert first["MMG_SNAPSHOT"] == "first"
    assert second["MMG_SNAPSHOT"] == "second"
