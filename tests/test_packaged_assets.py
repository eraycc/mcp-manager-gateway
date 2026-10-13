from pathlib import Path

from mcp_manager.config import PACKAGE_ROOT

ROOT = Path(__file__).parents[1]


def test_installed_package_contains_console_and_migrations():
    for name in ("index.html", "app.js", "core.js", "mcps.js", "importer.js", "styles.css"):
        assert (PACKAGE_ROOT / "static" / name).is_file(), name
    assert (PACKAGE_ROOT / "migrations" / "env.py").is_file()
    assert list((PACKAGE_ROOT / "migrations" / "versions").glob("*.py"))


def test_release_gate_and_mysql_matrix_cannot_skip():
    tests = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release-all.yml").read_text(encoding="utf-8")
    docker = (ROOT / ".github" / "workflows" / "docker-image.yml").read_text(encoding="utf-8")
    assert "workflow_call:" in tests
    assert "--db-backend=sqlite" in tests
    assert "--db-backend=mysql80" in tests
    assert "--db-backend=mysql84" in tests
    assert "--require-mysql" in tests
    assert "quality-gate:" in release
    assert "needs: [check-version, quality-gate]" in release
    assert "quality-gate:" in docker
    assert "needs: [check-version, quality-gate]" in docker


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
