import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect

from test_gateway import running_gateway  # noqa: F401, F811


@pytest.mark.asyncio
async def test_console_seven_routes_and_zero_jwt(running_gateway, tmp_path):  # noqa: F811
    app, web, url, token, row = running_gateway
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.get_by_label("用户名", exact=True).fill("admin")
        await page.get_by_label("密码", exact=True).fill("password12345")
        await page.get_by_role("button", name="登录", exact=True).click()
        await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()
        await expect(page.get_by_text("总调用次数", exact=True)).to_be_visible()
        screenshots = Path(__file__).parents[1] / "artifacts/qa"
        screenshots.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(screenshots / "dashboard-desktop.png"), full_page=True)
        for route, heading in [("mcps", "MCP 服务"), ("tokens", "访问令牌"), ("logs", "调用日志"),
                               ("users", "用户管理"), ("profile", "个人资料"), ("settings", "系统设置")]:
            await page.goto(url + "/#/" + route)
            await expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
            await expect(page.locator("#main .error:visible")).to_have_count(0)
        await page.get_by_label("登录有效期（天，0 为永不过期）").fill("0")
        await page.get_by_role("button", name="保存设置", exact=True).click()
        await expect(page.locator("#toast")).to_have_text("设置已保存")
        assert (await web.get("/api/v1/settings")).json()["jwt_days"] == 0
        await page.goto(url + "/#/profile")
        await page.get_by_label("邮箱", exact=True).fill("")
        await page.get_by_role("button", name="保存资料", exact=True).click()
        await expect(page.locator("#toast")).to_have_text("资料已保存")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.goto(url + "/#/dashboard")
        await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()
        await page.screenshot(path=str(screenshots / "dashboard-mobile.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert errors == []
        await browser.close()
