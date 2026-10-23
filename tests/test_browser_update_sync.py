"""About status and notification dots update without reloading the page."""
import asyncio

import test_browser_runtime_requirements as browser_fixtures
from playwright.async_api import expect

runtime_browser = browser_fixtures.runtime_browser
local_update_check = browser_fixtures.local_update_check


def update_state(current, latest, available):
    return {
        "current_version": current, "latest_version": latest, "ignored_version": "",
        "checked_at": "2026-10-23T00:00:00+00:00", "update_available": available,
        "notification_count": int(available), "check_error": "", "auto_check_enabled": True,
    }


async def test_manual_check_and_ignore_update_mounted_about_panel(runtime_browser):
    page, url = runtime_browser
    await page.goto(url + "/#/settings?settings_tab=about")
    about = page.get_by_role("tabpanel", name="关于", exact=True)
    await expect(about.get_by_role("button", name="检查更新", exact=True)).to_be_visible()
    state = update_state("1.4.9", "99.0.0", True)
    await page.route("**/api/v1/update-status/check", lambda route: route.fulfill(json=state))
    await about.get_by_role("button", name="检查更新", exact=True).click()
    await expect(about.locator(".about-version").nth(1).locator("strong")).to_have_text("99.0.0")
    await expect(about.locator(".update-status")).to_contain_text("发现新版本 99.0.0")
    for target in ('a[aria-label="系统设置"]', "#settings_tab-tab-about"):
        await expect(page.locator(target + " .notification-dot")).to_have_text("1")
    ignored = {**state, "ignored_version": "99.0.0", "update_available": False, "notification_count": 0}
    await page.route("**/api/v1/update-status/ignore", lambda route: route.fulfill(json=ignored))
    await about.get_by_role("button", name="跳过本次更新", exact=True).click()
    await expect(about.locator(".update-status")).to_contain_text("已忽略版本 99.0.0")
    await expect(page.locator(".notification-dot")).to_have_count(0)


async def test_automatic_result_clears_cached_upgrade_notice_without_reload(runtime_browser):
    page, url = runtime_browser
    release = asyncio.Event()
    current = (await (await page.request.get(url + "/api/v1/about")).json())["version"]
    await page.add_init_script("""localStorage.setItem("mcp-update-state", JSON.stringify({
        current_version:"0.0.1", latest_version:"99.0.0", update_available:true, notification_count:1
    }))""")

    async def delayed(route):
        await release.wait()
        await route.fulfill(json=update_state(current, current, False))

    await page.route("**/api/v1/update-status", delayed)
    try:
        await page.goto(url + "/?upgrade-test#/settings?settings_tab=about")
        about = page.get_by_role("tabpanel", name="关于", exact=True)
        await expect(about.get_by_role("button", name="检查更新", exact=True)).to_be_visible()
        await expect(about.locator(".about-version").nth(1).locator("strong")).to_have_text("99.0.0")
        await expect(about.locator(".about-version").nth(0).locator("strong")).to_have_text(current)
        release.set()
        await expect(about.locator(".about-version").nth(1).locator("strong")).to_have_text(current)
        await expect(about.locator(".update-status")).to_have_text("当前已经是最新版本。")
        await expect(page.locator(".notification-dot")).to_have_count(0)
    finally:
        release.set()
