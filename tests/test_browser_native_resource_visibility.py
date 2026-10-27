"""Resource compatibility controls stay compact, explicit and jointly saved."""
import os

from playwright.async_api import async_playwright, expect
from test_browser_profile_settings import login

pytest_plugins = ("test_browser_profile_settings",)


async def test_native_resource_visibility_joint_save_and_warning(running_gateway, tmp_path):
    _app, web, url, _, _ = running_gateway
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page(viewport={"width": 1280, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await login(page, url)
        await page.goto(url + "/#/settings")
        await page.get_by_role("tab", name="工具检索", exact=True).click()
        inject = page.get_by_label("启用内嵌资源注入", exact=True)
        native = page.get_by_label("对外暴露原生资源", exact=True)
        await expect(inject).not_to_be_checked()
        await expect(native).to_be_checked(timeout=3000)
        await inject.check()
        await native.uncheck()
        async with page.expect_response(lambda response: response.url.endswith("/api/v1/settings")
                                        and response.request.method == "PATCH") as pending:
            await page.get_by_role("button", name="保存资源兼容设置", exact=True).click()
        response = await pending.value
        assert response.status == 200
        assert response.request.post_data_json == {"embedded_resource.inject": True, "exposeNativeResources": False}
        await expect(page.locator("#toast")).to_contain_text("启用资源工具")
        saved = (await web.get("/api/v1/settings")).json()
        assert saved["embedded_resource.inject"] is True and saved["exposeNativeResources"] is False
        await page.reload()
        await expect(inject).to_be_checked()
        await expect(native).not_to_be_checked()
        await expect(page.get_by_text("已知 URI 仍可读取。", exact=False)).to_be_visible()
        section = page.locator("section").filter(has=page.get_by_role("heading", name="资源兼容", exact=True))
        await section.screenshot(path=str(tmp_path / "resource-compat-desktop.png"))
        await page.set_viewport_size({"width": 390, "height": 844})
        await section.screenshot(path=str(tmp_path / "resource-compat-mobile.png"))
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        await native.check()
        async with page.expect_response(lambda response: response.url.endswith("/api/v1/settings")
                                        and response.request.method == "PATCH") as pending:
            await page.get_by_role("button", name="保存资源兼容设置", exact=True).click()
        assert (await pending.value).status == 200
        assert (await web.get("/api/v1/settings")).json()["exposeNativeResources"] is True
        assert errors == []
        await browser.close()
