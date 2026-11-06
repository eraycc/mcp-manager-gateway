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
        await expect(page.get_by_role("button", name="保存设置", exact=True)).to_be_hidden()
        await expect(page.get_by_label("代理用户名", exact=True)).to_be_hidden()
        await expect(page.get_by_label("代理密码", exact=True)).to_be_hidden()
        await expect(page.get_by_role("group", name="自定义 CA 文件", exact=True)).to_be_hidden()
        await page.get_by_role("tab", name="基本设置", exact=True).click()
        await expect(page.get_by_role("button", name="保存设置", exact=True)).to_be_visible()
        await page.get_by_role("tab", name="网络与代理", exact=True).click()
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
        await expect(page.get_by_label("代理密码", exact=True)).to_be_visible()
        await page.get_by_label("代理认证", exact=True).select_option("none")
        await expect(page.get_by_label("代理用户名", exact=True)).to_be_hidden()
        await expect(page.get_by_label("代理密码", exact=True)).to_be_hidden()
        await page.get_by_label("代理认证", exact=True).select_option("basic")
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


async def test_proxy_ca_upload_visibility_persistence_and_removal(running_gateway):
    from test_outbound_ca import make_ca

    _app, web, url, _, _ = running_gateway
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await login(page, url)
        await page.goto(url + "/#/settings?settings_tab=network")
        await page.get_by_label("启用出站代理", exact=True).check()
        await page.get_by_label("代理主机", exact=True).fill("proxy.example")
        await page.get_by_label("目标名单", exact=True).fill("api.example.com\n192.0.2.0/24\n*.example.com\n")
        ssl = page.get_by_label("SSL 验证", exact=True)
        ca = page.get_by_role("group", name="自定义 CA 文件", exact=True)
        await expect(ca).to_be_hidden()
        await ssl.select_option("insecure")
        await expect(page.get_by_text("关闭 SSL 验证存在证书冒用风险", exact=False)).to_be_visible()
        await expect(ca).to_be_hidden()
        await ssl.select_option("custom_ca")
        await expect(ca).to_be_visible()
        await expect(page.get_by_label("自定义 CA 文件路径", exact=True)).to_have_count(0)
        upload = page.get_by_label("上传 CA 文件", exact=True)
        async with page.expect_response(lambda r: r.url.endswith("/outbound-proxy/ca") and r.request.method == "POST") as response:
            await upload.set_input_files({"name": "first.pem", "mimeType": "application/x-pem-file", "buffer": make_ca().encode()})
        assert (await response.value).status == 200
        await expect(ca).to_contain_text("proxy-ca.pem")
        await expect(ca).to_contain_text("上传")
        await expect(page.get_by_label("代理主机", exact=True)).to_have_value("proxy.example")
        await expect(page.get_by_label("启用出站代理", exact=True)).to_be_checked()
        await expect(ca.get_by_role("button", name="重新上传", exact=True)).to_be_visible()
        await expect(ca).to_contain_text("重新上传将覆盖当前 CA")
        saved_ca = (await web.get("/api/v1/settings/outbound-proxy")).json()["custom_ca"]

        async with page.expect_response(lambda r: r.url.endswith("/outbound-proxy/ca") and r.request.method == "POST") as response:
            await upload.set_input_files({"name": "invalid.pem", "mimeType": "application/x-pem-file", "buffer": b"invalid"})
        assert (await response.value).status == 422
        await expect(page.get_by_role("alert").filter(has_text="不是有效的 PEM CA 文件")).to_be_visible()
        assert (await web.get("/api/v1/settings/outbound-proxy")).json()["custom_ca"] == saved_ca

        async with page.expect_response(lambda r: r.url.endswith("/settings/outbound-proxy") and r.request.method == "PATCH") as response:
            await page.get_by_role("button", name="保存代理设置", exact=True).click()
        assert (await response.value).status == 200
        assert (await web.get("/api/v1/settings/outbound-proxy")).json()["list_entries"] == [
            "api.example.com", "192.0.2.0/24", "*.example.com"]
        await page.reload()
        await expect(ssl).to_have_value("custom_ca")
        await expect(ca).to_contain_text("proxy-ca.pem")
        await ssl.select_option("default")
        await expect(ca).to_be_hidden()
        await ssl.select_option("custom_ca")
        await expect(ca).to_contain_text("proxy-ca.pem")
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        async with page.expect_response(lambda r: r.url.endswith("/outbound-proxy/ca") and r.request.method == "DELETE") as response:
            await ca.get_by_role("button", name="移除 CA 文件", exact=True).click()
        assert (await response.value).status == 200
        await expect(ssl).to_have_value("default")
        await expect(ca).to_be_hidden()
        assert (await web.get("/api/v1/settings/outbound-proxy")).json()["custom_ca"] is None
        await browser.close()
