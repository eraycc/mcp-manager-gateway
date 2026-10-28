"""Selection guards must protect daily coverage and keep benchmarks opt-in."""
import json
from pathlib import Path
from types import SimpleNamespace

from suite_policy import affected_selection, explicit_path, layer_for, marker_requested

pytest_plugins = ["pytester"]


def test_layers_keep_small_capacity_regressions_and_static_budgets_daily():
    assert layer_for("test_runtime_capacity.py") == "unit"
    assert layer_for("test_password_capacity.py", ["db_app"]) == "integration"
    assert layer_for("performance/test_browser_budget.py") == "unit"
    assert layer_for("test_browser_large_proposals.py", ["db_app"]) == "integration"
    assert layer_for("performance/test_catalog_capacity.py") == "performance"
    assert layer_for("test_new_unclassified_regression.py") == "integration"
    assert layer_for("test_mysql_live.py", ["db_settings", "db_backend"]) == "integration"


def test_change_routing_is_conservative_and_includes_fixture_dependants():
    assert affected_selection(["src/mcp_manager/identity.py"]) is None
    assert affected_selection(["src/mcp_manager/embedding_models.py"]) is None
    assert affected_selection(["src/mcp_manager/oauth_config.py"]) is None
    assert affected_selection(["src/mcp_manager/new_module.py"]) is None
    assert affected_selection(["tests/conftest.py"]) is None
    domains, tests = affected_selection(["src/mcp_manager/static/capability-browser.js"])
    assert domains == {"ui"} and not tests
    domains, tests = affected_selection(["tests/test_gateway.py"])
    assert "test_browser_capability_text_wrap.py" in tests
    assert "test_gateway.py" in tests
    assert affected_selection(["README.md"]) == (set(), set())


def test_explicit_paths_do_not_treat_the_whole_test_tree_as_a_benchmark_request(tmp_path):
    key = "performance/test_catalog_capacity.py"
    assert not explicit_path(key, ["tests"], tmp_path)
    assert explicit_path(key, ["tests/performance"], tmp_path)
    assert explicit_path(key, ["tests/performance/test_catalog_capacity.py"], tmp_path)
    assert explicit_path(key, ["tests/performance/test_catalog_capacity.py::test_one"], tmp_path)
    assert explicit_path(key, ["tests/performance/test_catalog_capacity.py::test_one"], tmp_path, "test_one[x]")
    assert not explicit_path(key, ["tests/performance/test_catalog_capacity.py::test_other"], tmp_path, "test_one")


def test_real_pytest_selection_and_timing_contract(pytester, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parent))
    pytester.makeconftest('pytest_plugins = ("suite_policy",)\n')
    pytester.makepyfile("""
import pytest

@pytest.mark.unit
def test_daily():
    assert True

@pytest.mark.performance
def test_benchmark():
    assert True

@pytest.mark.live
def test_external():
    assert True
""")
    report = pytester.path / "timings.json"
    daily = pytester.runpytest_subprocess("-q", "-n", "0", "--timing-report", str(report))
    daily.assert_outcomes(passed=1, deselected=2)
    timings = json.loads(report.read_text(encoding="utf-8"))
    assert timings["executed"] == 1
    assert next(iter(timings["tests"].values()))["layer"] == "unit"
    assert set(next(iter(timings["tests"].values()))) >= {"setup", "call", "teardown"}
    benchmark = pytester.runpytest_subprocess("-q", "-n", "0", "--suite", "performance")
    benchmark.assert_outcomes(passed=1, deselected=2)
    marked = pytester.runpytest_subprocess("-q", "-n", "0", "-m", "performance")
    marked.assert_outcomes(passed=1, deselected=2)
    complete = pytester.runpytest_subprocess("-q", "-n", "0", "--suite", "all")
    complete.assert_outcomes(passed=2, deselected=1)
    flagged = pytester.runpytest_subprocess("-q", "-n", "0", "--run-performance")
    flagged.assert_outcomes(passed=2, deselected=1)
    live = pytester.runpytest_subprocess("-q", "-n", "0", "--run-live", "-m", "live")
    live.assert_outcomes(passed=1, deselected=2)


def test_negative_marker_expressions_never_opt_in_to_stress_tests():
    def config(expression):
        return SimpleNamespace(option=SimpleNamespace(markexpr=expression))
    assert not marker_requested(config("not performance"), "performance")
    assert not marker_requested(config("not (performance and catalog)"), "performance")
    assert marker_requested(config("performance and not browser"), "performance")
    assert marker_requested(config("not (not performance)"), "performance")
