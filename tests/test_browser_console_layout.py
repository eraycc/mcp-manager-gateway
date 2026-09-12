"""Localhost console login and compact responsive basic settings."""
import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_browser_profile_settings import running_gateway, login  # noqa: F401


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
        token = page.get_by_label("启用令牌认证", exact=True)
        cors = page.get_by_label("允许跨域接入的来源", exact=True)
        days = page.get_by_label("登录有效期（天，0 为永不过期）", exact=True)
        shots = Path(__file__).parents[1] / "artifacts/qa"
        shots.mkdir(parents=True, exist_ok=True)
        for width in (1440, 390, 320):
            await page.set_viewport_size({"width": width, "height": 1000})
            await expect(title).to_be_visible()
            await expect(page.locator(".shell")).to_have_css("margin-left", "240px" if width > 720 else "0px")
            a, b, c, d, e = [await item.bounding_box() for item in (title, registration, token, days, cors)]
            assert 0 <= b["y"] - a["y"] - a["height"] <= 28
            assert e["y"] > max(b["y"] + b["height"], c["y"] + c["height"], d["y"] + d["height"])
            if width > 720:
                assert abs(b["y"] - c["y"]) <= 1
            else:
                assert c["y"] > b["y"]
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            await page.screenshot(path=str(shots / f"settings-basic-{width}.png"), full_page=True)
        await title.fill("Localhost console")
        await page.get_by_role("button", name="保存设置", exact=True).click()
        await expect(page.locator("#toast")).to_have_text("设置已保存")
        assert (await web.get("/api/v1/settings")).json()["title"] == "Localhost console"
        await browser.close()
