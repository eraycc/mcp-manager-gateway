from pathlib import Path

from mcp_manager.config import PACKAGE_ROOT

ROOT = Path(__file__).parents[1]


def test_installed_package_contains_console_and_migrations():
    for name in ("index.html", "app.js", "core.js", "mcps.js", "importer.js", "json-worker.js", "styles.css"):
        assert (PACKAGE_ROOT / "static" / name).is_file(), name
    assert (PACKAGE_ROOT / "migrations" / "env.py").is_file()
    assert list((PACKAGE_ROOT / "migrations" / "versions").glob("*.py"))


def test_public_docs_link_database_deployment_without_private_paths():
    paths = [ROOT / "README.md", ROOT / "README.zh-CN.md", *sorted((ROOT / "wiki").glob("*.md"))]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "C:\\Users\\" not in text, path
        assert "/Users/" not in text, path
        assert "mygain3d" not in text.lower(), path
    assert "database-deployment.md" in (ROOT / "wiki" / "README.md").read_text(encoding="utf-8")
    assert "wiki/database-deployment.md" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "wiki/database-deployment.md" in (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
