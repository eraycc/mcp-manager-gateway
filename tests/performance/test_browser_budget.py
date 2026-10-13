"""The console has bounded rendering and accessible loading feedback."""

from importlib.resources import files


def static_text(name):
    return files("mcp_manager").joinpath("static", name).read_text(encoding="utf-8")


def test_browser_loading_and_render_budget_contract():
    core = static_text("core.js")
    capabilities = static_text("capability-browser.js")
    styles = static_text("styles.css")
    assert "CAPABILITY_PAGE_SIZE=50" in capabilities
    assert "aria-busy':'true'" in core
    assert "skeleton-lines" in core
    assert "control.setAttribute('aria-busy','true')" in capabilities
    assert "body.setAttribute('aria-busy','true')" in capabilities
    assert "@media(prefers-reduced-motion:reduce)" in styles
    assert ".loading-state" in styles
    assert ".is-busy" in styles
