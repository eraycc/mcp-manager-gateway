"""About settings keep the cache interval editable regardless of the automatic-check toggle."""
import pytest
import test_browser_runtime_requirements as browser_fixtures
from playwright.async_api import expect

runtime_browser = browser_fixtures.runtime_browser
local_update_check = browser_fixtures.local_update_check


@pytest.mark.asyncio
async def test_update_interval_persists_with_auto_check_on_or_off(runtime_browser):
    page, url = runtime_browser
    await page.goto(url + "/#/settings?settings_tab=about")
    about = page.get_by_role("tabpanel", name="关于", exact=True)
    toggle = about.get_by_label("自动检测更新", exact=True)
    interval = about.get_by_label("自动检测间隔（秒）", exact=True)
    await expect(interval).to_have_value("86400")
    await expect(interval).to_be_enabled()
    await expect(interval).to_have_attribute("min", "1")
    hint = about.locator("#settings-hint-update_check_interval_seconds")
    for text in ("PyPI 最新版本信息", "缓存有效时长", "下次登录、打开关于或刷新", "读取缓存", "强制联网"):
        await expect(hint).to_contain_text(text)
    await interval.fill("0")
    assert not await interval.evaluate("(n)=>n.checkValidity()")
    await interval.fill("3600")
    async with page.expect_response(lambda r: r.url.endswith("/api/v1/settings") and r.request.method == "PATCH") as saved:
        await page.get_by_role("button", name="保存设置", exact=True).click()
    assert (await saved.value).status == 200
    await page.reload()
    await expect(interval).to_have_value("3600")
    await toggle.uncheck()
    await expect(interval).to_be_enabled()
    await interval.fill("600")
    async with page.expect_response(lambda r: r.url.endswith("/api/v1/settings") and r.request.method == "PATCH") as saved:
        await page.get_by_role("button", name="保存设置", exact=True).click()
    response = await saved.value
    assert response.status == 200
    assert response.request.post_data_json["update_check_interval_seconds"] == 600
    await page.reload()
    await expect(toggle).not_to_be_checked()
    await expect(interval).to_be_enabled()
    await expect(interval).to_have_value("600")
    await expect(about.get_by_role("button", name="检查更新", exact=True)).to_be_enabled()
    await toggle.check()
    await expect(interval).to_be_enabled()
    await expect(interval).to_have_value("600")
