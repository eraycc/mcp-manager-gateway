"""Focused UI acceptance for physical space maintenance (browser group runs last)."""
import asyncio

import pytest
import test_browser_bug1 as browser_fixtures
from playwright.async_api import expect

storage_browser = browser_fixtures.bug1_browser


@pytest.mark.asyncio
async def test_storage_settings_save_loading_and_manual_reclamation(storage_browser):
    page, url = storage_browser
    await page.goto(url + "/#/settings?settings_tab=storage")
    panel = page.get_by_role("tabpanel", name="存储治理", exact=True)
    main = panel.get_by_role("group", name="主 SQLite", exact=True)
    logs = panel.get_by_role("group", name="日志索引 SQLite", exact=True)
    await expect(main).to_be_visible()
    await expect(logs).to_be_visible()
    await main.get_by_label("最小空闲空间（MiB）", exact=True).fill("64")
    await main.get_by_label("最小空闲比例（%）", exact=True).fill("30")
    await main.get_by_role("button", name="保存回收设置", exact=True).click()
    await expect(main.get_by_role("status")).to_contain_text("回收设置已保存")
    await expect(logs.get_by_label("最小空闲空间（MiB）", exact=True)).to_have_value("32")
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(route):
        entered.set()
        await release.wait()
        await route.continue_()
    await page.route("**/api/v1/settings/storage/logs/vacuum", delayed)
    control = logs.get_by_role("button", name="释放数据库空间", exact=True)
    await control.click()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await expect(control).to_be_disabled()
        await expect(control).to_have_attribute("aria-busy", "true")
        await expect(logs.get_by_role("status")).to_contain_text("正在")
    finally:
        release.set()
    await expect(logs.get_by_role("status")).to_contain_text("已释放", timeout=15000)
    await expect(control).to_be_enabled()
    await expect(control).not_to_have_attribute("aria-busy", "true")
    await page.reload()
    main = page.get_by_role("group", name="主 SQLite", exact=True)
    await expect(main.get_by_label("最小空闲空间（MiB）", exact=True)).to_have_value("64")


@pytest.mark.asyncio
async def test_mysql_hides_main_sqlite_controls_but_keeps_log_index(storage_browser):
    page, url = storage_browser
    async def mysql(route):
        response = await route.fetch()
        data = await response.json()
        data["databases"]["main"] = {
            "supported": False, "backend": "mysql", "policy": None,
            "stats": None, "running": False, "last_result": None,
        }
        await route.fulfill(json=data)
    await page.route("**/api/v1/settings/storage", mysql)
    await page.goto(url + "/#/settings?settings_tab=storage")
    panel = page.get_by_role("tabpanel", name="存储治理", exact=True)
    await expect(panel.get_by_text("主数据库使用 MySQL", exact=False)).to_be_visible()
    await expect(panel.get_by_role("group", name="主 SQLite", exact=True)).to_have_count(0)
    await expect(panel.get_by_role("button", name="释放数据库空间", exact=True)).to_have_count(1)
    await expect(panel.get_by_role("group", name="日志索引 SQLite", exact=True)).to_be_visible()
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
