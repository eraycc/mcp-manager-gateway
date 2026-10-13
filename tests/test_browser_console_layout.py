"""Localhost console login and compact responsive basic settings."""
import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_browser_profile_settings import login, running_gateway  # noqa: F401


@pytest.mark.asyncio
async def test_localhost_login_and_basic_settings_layout(running_gateway):  # noqa: F811
    _app, web, configured_url, _, _ = running_gateway
    url = configured_url.replace("127.0.0.1", "localhost")
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        await login(page, url)
        await page.goto(url + "/#/settings")
        title = page.get_by_label("站点名称", exact=True)
        registration = page.get_by_label("允许注册", exact=True)
        cors = page.get_by_label("允许跨域接入的来源", exact=True)
        days = page.get_by_label("登录有效期（天，0 为永不过期）", exact=True)
        shots = Path(__file__).parents[1] / "artifacts/qa"
        shots.mkdir(parents=True, exist_ok=True)
        for width in (1440, 390, 320):
            await page.set_viewport_size({"width": width, "height": 1000})
            await expect(title).to_be_visible()
            await expect(page.locator(".shell")).to_have_css("margin-left", "240px" if width > 720 else "0px")
            a, b, c, d = [await item.bounding_box() for item in (title, registration, days, cors)]
            assert 0 <= b["y"] - a["y"] - a["height"] <= 28
            assert d["y"] > max(b["y"] + b["height"], c["y"] + c["height"])
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            await page.screenshot(path=str(shots / f"settings-basic-{width}.png"), full_page=True)
        await page.get_by_role("tab", name="匿名访问", exact=True).click()
        anonymous = page.get_by_label("允许匿名访问", exact=True)
        await expect(anonymous).to_be_visible()
        await expect(anonymous).not_to_be_checked()
        await anonymous.check()
        await page.get_by_role("tab", name="基本设置", exact=True).click()
        await title.fill("Localhost console")
        await page.get_by_role("button", name="保存设置", exact=True).click()
        await expect(page.locator("#toast")).to_have_text("设置已保存")
        saved = (await web.get("/api/v1/settings")).json()
        assert saved["title"] == "Localhost console"
        assert saved["token_auth_enabled"] is False
        await browser.close()
