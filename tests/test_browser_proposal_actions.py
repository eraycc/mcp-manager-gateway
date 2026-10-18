"""Finalized proposals must not offer an expired diagnostic catalog."""
import os

from playwright.async_api import async_playwright, expect
from test_browser_profile_settings import login, running_gateway  # noqa: F401


async def test_capability_action_only_for_unfinalized_successful_proposals(running_gateway):  # noqa: F811
    _app, _web, url, _token, _row = running_gateway
    rows = [
        {"id": status, "name": status, "status": status, "transport": "stdio",
         "test_status": "success", "test_result": {"tool_count": 1}}
        for status in ("pending", "incomplete", "approved", "rejected")
    ]
    rows.append({"id": "failed", "name": "failed", "status": "pending",
                 "transport": "stdio", "test_status": "failed"})
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(
            headless=True, executable_path=executable if os.path.exists(executable) else None,
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.route("**/api/v1/mcp-proposals?*", lambda route: route.fulfill(json={
            "items": rows, "total": len(rows), "total_pages": 1,
        }))
        await page.route("**/api/v1/mcp-proposals/*/test-capabilities?*", lambda route: route.fulfill(json={
            "items": [{"name": "read"}], "total": 1, "next_cursor": None,
        }))
        await login(page, url)
        await page.goto(url + "/#/proposals")
        await expect(page.locator("tbody tr")).to_have_count(len(rows))
        for status in ("approved", "rejected"):
            row = page.locator(f'tr[data-row-id="{status}"]')
            await expect(row.get_by_role("button", name="能力目录", exact=True)).to_have_count(0)
            await expect(row.locator(".actions button .button-label")).to_have_text(["详情", "删除"])
        for status in ("pending", "incomplete"):
            row = page.locator(f'tr[data-row-id="{status}"]')
            await row.get_by_role("button", name="能力目录", exact=True).click()
            popup = page.get_by_role("dialog", name=status + " · 测试能力", exact=True)
            await expect(popup.locator(".capability-card")).to_contain_text("read")
            await popup.get_by_role("button", name="关闭", exact=True).click()
        await expect(page.locator('tr[data-row-id="failed"]').get_by_role(
            "button", name="能力目录", exact=True
        )).to_have_count(0)
        await browser.close()
