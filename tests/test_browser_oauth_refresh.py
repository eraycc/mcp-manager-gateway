"""Refreshing OAuth credentials updates the dialog without invoking tools."""
import test_browser_runtime_requirements as browser_fixtures
from playwright.async_api import expect

runtime_browser = browser_fixtures.runtime_browser
local_update_check = browser_fixtures.local_update_check


async def test_oauth_refresh_action_updates_authorization_status(runtime_browser):
    page, _url = runtime_browser
    state = {
        "authorized": False, "state": "expired", "auth_status": "expired",
        "refresh_available": True, "isolation": "user", "config_isolation": "shared",
    }
    await page.route("**/api/v1/mcps/fixture/oauth/status", lambda route: route.fulfill(json=state))

    async def refreshed(route):
        assert route.request.method == "POST"
        state.update(authorized=True, state="ready", auth_status="valid")
        await route.fulfill(json=state)

    await page.route("**/api/v1/mcps/fixture/oauth/refresh", refreshed)
    await page.evaluate("async()=>{const m=await import('/mcps.js');await m.oauthDialog({id:'fixture'},false)}")
    popup = page.get_by_role("dialog", name="授权服务", exact=True)
    await expect(popup).to_contain_text("当前登录用户尚未完成 OAuth 授权")
    await popup.get_by_role("button", name="刷新授权", exact=True).click()
    await expect(popup).to_contain_text("当前登录用户已完成 OAuth 授权。")
    await expect(popup.get_by_role("button", name="断开 OAuth 授权", exact=True)).to_be_visible()
