"""Pytest selection, conservative change routing and per-phase timing reports."""
import ast
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from suite_manifest import (
    BROWSER_FILES,
    DOMAINS,
    LIVE_FILES,
    PERFORMANCE_FILES,
    SMOKE_NODES,
    TEST_IMPORTS,
    UNIT_FILES,
)

LAYERS = ("unit", "integration", "browser", "performance", "live")
DOMAIN_NAMES = ("auth", "catalog", "deployment", "gateway", "imports", "oauth", "runtime", "storage", "ui", "core")
SUITES = {
    "daily": {"unit", "integration", "browser"},
    "quick": {"unit"},
    "backend": {"unit", "integration"},
    "browser": {"browser"},
    "performance": {"performance"},
    "all": {"unit", "integration", "browser", "performance"},
}
DB_FIXTURES = {"db_app", "dual_db", "db_settings", "running_gateway", "clients", "client", "indexed_app"}


def pytest_addoption(parser):
    group = parser.getgroup("MMG test suites")
    group.addoption("--suite", choices=tuple(SUITES), default="daily", help="Daily excludes performance/live.")
    group.addoption("--run-performance", action="store_true", help="Include opt-in performance tests.")
    group.addoption("--run-live", action="store_true", help="Include live tests; credentials are still required.")
    group.addoption("--domain", action="append", choices=DOMAIN_NAMES, default=[], help="Union of business domains.")
    group.addoption("--changed-from", metavar="GIT_REF", help="Conservatively select tests affected since this ref.")
    group.addoption("--timing-report", metavar="PATH", help="Write per-test setup/call/teardown JSON here.")


def test_key(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    try:
        key = path.relative_to(root).as_posix()
    except ValueError:
        return path.name
    return key.removeprefix("tests/")


def layer_for(key, fixture_names=()):
    if key in PERFORMANCE_FILES:
        return "performance"
    if key in LIVE_FILES:
        return "live"
    if key in BROWSER_FILES:
        return "browser"
    if key in UNIT_FILES and not DB_FIXTURES.intersection(fixture_names):
        return "unit"
    return "integration"


def domains_for(key):
    return set(DOMAINS.get(key, DOMAIN_NAMES))


def explicit_path(key, args, root, node_name=None):
    target = (Path(root) / "tests" / key).resolve()
    for arg in args:
        path, *node = str(arg).split("::", 1)
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = Path(root) / resolved
        resolved = resolved.resolve()
        if resolved == target and (not node or node_name is None or node_name.startswith(node[0])):
            return True
        # A named performance directory is an explicit request; tests/ is not.
        if key in PERFORMANCE_FILES and resolved == target.parent:
            return True
    return False


def marker_requested(config, name):
    # A negative marker mention is not an opt-in. Parse Boolean structure without
    # executing the expression; pytest applies the complete marker filter later.
    expression = config.option.markexpr or ""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return False
    def positive(node, negated=False):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return positive(node.operand, not negated)
        if isinstance(node, ast.Name):
            return node.id == name and not negated
        return any(positive(child, negated) for child in ast.iter_child_nodes(node))
    return positive(tree)


def wants_performance(config):
    return (
        config.getoption("run_performance") or config.getoption("suite") in {"performance", "all"}
        or marker_requested(config, "performance")
        or any(explicit_path(key, config.args, config.rootpath) for key in PERFORMANCE_FILES)
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    for name in (*LAYERS, *DOMAIN_NAMES, "smoke", "mysql"):
        config.addinivalue_line("markers", name + ": MMG suite or business-domain classification")
    config._mmg_timings = {}
    config._mmg_started = time.perf_counter()
    # Respect -n 0/-n N, debugging, and collection. Bound concurrency rather than
    # spawning one memory-heavy browser/Argon2 worker per logical CPU.
    if (not hasattr(config, "workerinput") and config.pluginmanager.hasplugin("xdist")
            and config.option.numprocesses is None and not config.option.collectonly):
        count = 0 if wants_performance(config) or config.option.usepdb else min(4, os.cpu_count() or 1)
        if count and config.option.maxprocesses:
            count = min(count, config.option.maxprocesses)
        config.option.numprocesses = count
        if count:
            if config.option.dist == "no":
                config.option.dist = "worksteal"
            config.option.tx = ["popen"] * count


def affected_selection(paths):
    """Unknown or foundational changes expand to every daily regression."""
    selected_domains, selected_tests = set(), set()
    foundation = {
        "app", "database", "db_types", "config", "environment", "identity", "credentials",
        "transports", "runtime", "schema_validation", "catalog_types", "catalog_models",
    }
    module_groups = {
        "oauth": {"oauth", "auth", "catalog", "gateway", "ui"},
        "catalog": {"catalog", "gateway", "oauth", "ui"},
        "discovery": {"catalog", "gateway", "oauth", "ui"},
        "embedding": {"catalog", "gateway"},
        "resource": {"catalog", "gateway", "ui"},
        "proposal": {"catalog", "gateway", "ui"},
        "import": {"imports", "catalog", "ui"},
        "job": {"storage", "ui"},
        "audit": {"storage", "auth", "ui"},
        "log": {"storage", "gateway", "ui"},
        "storage": {"storage", "ui"},
        "bridge": {"runtime", "gateway"},
        "lease": {"runtime", "gateway", "ui"},
        "paging": {"catalog", "gateway", "ui"},
        "retention": {"storage", "runtime"},
        "maintenance": {"storage", "runtime", "catalog"},
        "about": {"deployment", "ui"},
        "update": {"deployment", "ui"},
        "translation": {"ui"},
        "cors": {"auth", "gateway", "ui"},
        "diagnostic": {"auth", "catalog", "gateway"},
    }
    for raw in paths:
        path = raw.replace("\\", "/")
        if path.startswith(("docs/", "wiki/")) or Path(path).name in {"README.md", "README.zh-CN.md"}:
            continue
        if path.startswith("tests/"):
            key = path.removeprefix("tests/")
            if key not in DOMAINS:
                return None
            selected_tests.add(key)
            continue
        if path.startswith("src/mcp_manager/static/"):
            selected_domains.add("ui")
            continue
        if not path.startswith("src/mcp_manager/") or "/migrations/" in path:
            return None
        module = Path(path).stem
        if module in foundation or module.endswith(("_models", "_config")):
            return None
        matches = [values for prefix, values in module_groups.items() if module.startswith(prefix)]
        if not matches:
            return None
        selected_domains.update(set.union(*matches))
    # Test modules also provide fixtures to other modules; include dependants.
    while True:
        dependants = {key for key, imports in TEST_IMPORTS.items() if selected_tests.intersection(imports)}
        if dependants <= selected_tests:
            break
        selected_tests.update(dependants)
    return selected_domains, selected_tests


def changed_paths(config):
    ref = config.getoption("changed_from")
    if not ref:
        return None
    def git(*args, nul=False):
        result = subprocess.run(["git", *args], cwd=config.rootpath, capture_output=True, text=True, encoding="utf-8", check=False)
        if result.returncode:
            raise pytest.UsageError("Cannot resolve --changed-from; no tests were silently excluded.")
        return [path for path in result.stdout.split("\0") if path] if nul else result.stdout.splitlines()
    commit = git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}")[0]
    return git("diff", "--name-only", "-z", commit, "--", nul=True) + git(
        "ls-files", "--others", "--exclude-standard", "-z", nul=True
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    affected = affected_selection(changed_paths(config)) if config.getoption("changed_from") else None
    kept, removed = [], []
    for item in items:
        key = test_key(item.path, config.rootpath)
        declared = [name for name in LAYERS if item.get_closest_marker(name)]
        if len(declared) > 1:
            raise pytest.UsageError("Conflicting test layers: " + item.nodeid)
        layer = declared[0] if declared else layer_for(key, item.fixturenames)
        domains = domains_for(key) | {name for name in DOMAIN_NAMES if item.get_closest_marker(name)}
        item.add_marker(getattr(pytest.mark, layer))
        for domain in sorted(domains):
            item.add_marker(getattr(pytest.mark, domain))
        stem = item.nodeid.split("[", 1)[0].removeprefix("tests/")
        smoke = stem in SMOKE_NODES or bool(item.get_closest_marker("smoke"))
        if smoke:
            item.add_marker(pytest.mark.smoke)
        item.user_properties.extend([("mmg_layer", layer), ("mmg_domains", ",".join(sorted(domains)))])
        suite = config.getoption("suite")
        chosen = layer in SUITES[suite] or (suite == "quick" and smoke)
        if layer in {"performance", "live"}:
            requested = (
                config.getoption("run_" + layer) or marker_requested(config, layer)
                or explicit_path(key, config.args, config.rootpath, item.nodeid.split("::", 1)[-1])
                or (layer == "live" and config.getoption("require_mysql", default=False) and key == "test_mysql_live.py")
            )
            chosen = chosen or requested
            if layer == "live":
                chosen = requested  # Even --suite all never starts external services implicitly.
        selected_domains = set(config.getoption("domain"))
        chosen = chosen and (not selected_domains or bool(domains & selected_domains))
        if affected is not None:
            changed_domains, changed_tests = affected
            relevant = bool(domains & changed_domains) or key in changed_tests
            if not changed_domains and not changed_tests:
                relevant = layer == "unit" or smoke
            chosen = chosen and relevant
        (kept if chosen else removed).append(item)
    items[:] = kept
    if removed:
        config.hook.pytest_deselected(items=removed)


def pytest_runtest_logreport(report):
    # Pytest carries item.user_properties across the xdist report channel.
    state = _timing_state
    if state is None:
        return
    properties = dict(report.user_properties)
    value = state.setdefault(report.nodeid, {
        "layer": properties.get("mmg_layer", "integration"),
        "domains": properties.get("mmg_domains", "").split(","),
        "setup": 0.0, "call": 0.0, "teardown": 0.0, "outcome": "passed",
    })
    value[report.when] += report.duration
    if report.failed or report.skipped:
        value["outcome"] = report.outcome


_timing_state = None


def pytest_sessionstart(session):
    global _timing_state
    if not hasattr(session.config, "workerinput"):
        _timing_state = session.config._mmg_timings


def pytest_sessionfinish(session, exitstatus):
    global _timing_state
    config = session.config
    if hasattr(config, "workerinput") or config.option.collectonly:
        return
    report = {
        "suite": config.getoption("suite"), "exitstatus": int(exitstatus),
        "elapsed_seconds": round(time.perf_counter() - config._mmg_started, 3),
        "executed": len(config._mmg_timings), "tests": config._mmg_timings,
    }
    destination = config.getoption("timing_report")
    path = Path(destination) if destination else config.cache.mkdir("mmg") / "timings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config._mmg_timing_path = path
    _timing_state = None


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if hasattr(config, "_mmg_timing_path"):
        terminalreporter.write_line(
            f"MMG suite={config.getoption('suite')}; per-phase timings: {config._mmg_timing_path}"
        )
