"""Audit management in a real browser against an isolated temporary gateway."""
import os

from playwright.async_api import async_playwright, expect
from test_browser_profile_settings import login

pytest_plugins = ("test_browser_profile_settings",)


async def test_audit_management_filters_details_exports_and_deletion(running_gateway, tmp_path):
    app, web, url, _, _ = running_gateway
    identity = await app.state.logs.audit("mcp.create", None, {"mcp_id": "audit-qa"},
                                          target_type="mcp", target_id="audit-qa", target_name="Audit QA MCP")
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await login(page, url)
        await page.get_by_role("link", name="审计日志", exact=True).click()
        await expect(page.get_by_role("heading", name="审计日志", exact=True)).to_be_visible()
        await expect(page.get_by_text("无限保留", exact=False).first).to_be_visible()
        search = page.get_by_role("searchbox", name="搜索", exact=True)
        await search.fill("Audit QA")
        await page.get_by_role("button", name="搜索", exact=True).click()
        row = page.locator("tr").filter(has_text="Audit QA MCP")
        await expect(row).to_have_count(1)
        await row.get_by_role("button", name="详情", exact=True).click()
        await expect(page.get_by_role("dialog", name="审计详情", exact=True)).to_contain_text(identity)
        await page.get_by_role("dialog", name="审计详情", exact=True).get_by_role("button", name="关闭", exact=True).click()
        async with page.expect_download() as pending:
            await page.get_by_role("button", name="导出 JSON", exact=True).click()
        download = await pending.value
        assert download.suggested_filename == "audit-logs.json"
        await page.locator("#main").screenshot(path=str(tmp_path / "audit-desktop.png"))
        await row.get_by_role("checkbox").check()
        await page.get_by_role("button", name="删除选中", exact=True).click()
        async with page.expect_response(lambda response: response.url.endswith("/api/v1/audit-logs/delete")
                                        and response.request.method == "POST") as pending:
            await page.get_by_role("button", name="确认执行", exact=True).click()
        assert (await pending.value).status == 200
        await expect(row).to_have_count(0, timeout=15000)
        assert (await web.get("/api/v1/audit-logs/" + identity)).status_code == 404
        await page.goto(url + "/#/audit-logs")
        await page.set_viewport_size({"width": 390, "height": 844})
        await expect(page.get_by_role("heading", name="审计日志", exact=True)).to_be_visible()
        await page.locator("#main").screenshot(path=str(tmp_path / "audit-mobile.png"))
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert errors == []
        await browser.close()


async def test_non_admin_cannot_open_audit_page(running_gateway):
    _app, web, url, _, _ = running_gateway
    response = await web.post("/api/v1/auth/register", json={"username": "ordinary", "password": "password12345"})
    assert response.status_code == 200
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.goto(url)
        await page.locator('input[autocomplete="username"]').fill("ordinary")
        await page.locator('input[autocomplete="current-password"]').fill("password12345")
        await page.locator(".auth form button[type=submit]").click()
        await expect(page.locator(".stats")).to_be_visible()
        await expect(page.get_by_role("link", name="审计日志", exact=True)).to_have_count(0)
        await page.goto(url + "/#/audit-logs")
        await expect(page).to_have_url(url + "/#/dashboard")
        await browser.close()
