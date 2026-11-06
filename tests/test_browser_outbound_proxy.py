"""Network settings preserve failed input and persist only validated candidates."""
import os

from playwright.async_api import async_playwright, expect
from test_browser_profile_settings import login

pytest_plugins = ("test_browser_profile_settings",)


async def test_proxy_settings_disabled_validation_and_persistence(running_gateway, monkeypatch):
    from mcp_manager import outbound

    _app, web, url, _, _ = running_gateway
    calls = []
    async def validate(config):
        calls.append(config)
        if config["host"] == "invalid.example":
            raise outbound.ProxyDiagnosticError("无法连接到代理服务器，请检查 host/port")
    monkeypatch.setattr(outbound, "validate_connection", validate)
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await login(page, url)
        await page.goto(url + "/#/settings")
        await page.get_by_role("tab", name="网络与代理", exact=True).click()
        host = page.get_by_label("代理主机", exact=True)
        await expect(host).to_be_disabled()
        await page.get_by_label("启用出站代理", exact=True).check()
        await expect(host).to_be_enabled()
        await host.fill("invalid.example")
        async with page.expect_response(lambda r: r.url.endswith("/settings/outbound-proxy/test") and r.request.method == "POST"):
            await page.get_by_role("button", name="测试代理连接", exact=True).press("Enter")
        await expect(page.get_by_role("status").filter(has_text="无法连接")).to_be_visible()
        await expect(host).to_have_value("invalid.example")
        assert (await web.get("/api/v1/settings/outbound-proxy")).json()["enabled"] is False
        await host.fill("proxy.example")
        await page.get_by_label("代理认证", exact=True).select_option("basic")
        await page.get_by_label("代理用户名", exact=True).fill("user")
        async with page.expect_response(lambda r: r.url.endswith("/settings/outbound-proxy") and r.request.method == "PATCH") as response:
            await page.get_by_role("button", name="保存代理设置", exact=True).click()
        assert (await response.value).status == 200
        await page.reload()
        await expect(host).to_have_value("proxy.example")
        await expect(page.get_by_label("启用出站代理", exact=True)).to_be_checked()
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert len(calls) == 1
        assert not errors
        await browser.close()
