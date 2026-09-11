from mcp_manager.config import PACKAGE_ROOT


def test_installed_package_contains_console_and_migrations():
    for name in ("index.html", "app.js", "core.js", "mcps.js", "importer.js", "styles.css"):
        assert (PACKAGE_ROOT / "static" / name).is_file(), name
    assert (PACKAGE_ROOT / "migrations" / "env.py").is_file()
    assert list((PACKAGE_ROOT / "migrations" / "versions").glob("*.py"))
