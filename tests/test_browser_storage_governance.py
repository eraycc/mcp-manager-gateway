"""Real UI policy persistence, permanent-retention visibility and narrow viewport."""
import test_browser_bug1 as browser_fixtures
from playwright.async_api import expect

storage_browser = browser_fixtures.bug1_browser


async def test_governance_policy_save_and_reload(storage_browser):
    page, url = storage_browser
    await page.goto(url + "/#/settings?settings_tab=storage")
    group = page.get_by_role("group", name="容量与清理策略", exact=True)
    await expect(group.get_by_text("永久保存（0）", exact=False)).to_be_visible()
    await group.get_by_text("调整容量、内存与后台清理预算", exact=True).click()
    await group.get_by_label("缓存正文容量（MiB）", exact=True).fill("64")
    async with page.expect_response(lambda r: r.url.endswith("/settings/storage/governance") and r.request.method == "PATCH") as saved:
        await group.get_by_role("button", name="保存治理策略", exact=True).click()
    assert (await saved.value).ok
    await expect(group.get_by_role("status")).to_contain_text("治理策略已保存并生效")
    await page.reload()
    group = page.get_by_role("group", name="容量与清理策略", exact=True)
    await group.get_by_text("调整容量、内存与后台清理预算", exact=True).click()
    await expect(group.get_by_label("缓存正文容量（MiB）", exact=True)).to_have_value("64")
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
