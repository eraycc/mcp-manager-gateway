"""Audit management in a real browser against an isolated temporary gateway."""
import json
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


async def test_legacy_audit_json_and_long_columns_stay_bounded(running_gateway, tmp_path):
    app, _web, url, _, _ = running_gateway
    long_name = "历史操作人-" + "account-" * 40
    long_ip = "ffff:ffff:ffff:ffff:ffff:ffff:255.255.255.255"
    records = [
        {"event_id": "legacy-lease-overflow", "action": "lease.disconnect", "details": {
            "lease_id": "499a8fa10c17450f936be969ea024c58", "ok": True, "released": True,
            "already_released": False, "session_cleanup_ok": True,
            "instances": [{"name": "historical-instance-" * 20, "phase": "stopped"} for _ in range(5)]}},
        {"event_id": "legacy-storage-overflow", "action": "storage.startup", "details": {
            "database": "main", "before": {"page_bytes": 64917504, "free_bytes": 41172992,
            "free_percent": 63.42355984604707, "diagnostic": "historical-storage-detail-" * 30},
            "after": {"page_bytes": 23744512, "free_bytes": 0}}},
    ]
    legacy = app.state.logs.audits.root / "2026-09-11.jsonl"
    legacy.write_text("".join(json.dumps(record | {
        "timestamp": "2026-09-11T01:00:00Z", "user_id": "legacy-user", "username": long_name,
        "target_name": "历史对象-" + "target-" * 40, "ip": long_ip,
    }, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        await login(page, url)
        await page.goto(url + "/#/audit-logs")
        for width in [1440, 390, 320]:
            await page.set_viewport_size({"width": width, "height": 1000})
            for record in records:
                row = page.locator('tr[data-row-id="' + record["event_id"] + '"]')
                await expect(row).to_be_visible()
                bounds = await row.evaluate("""row => {
                    const table = row.closest('table'), scroll = table.parentElement;
                    const summary = row.cells[7].firstElementChild, ip = row.cells[6].firstElementChild;
                    return {
                        table: table.getBoundingClientRect().width, container: scroll.clientWidth,
                        summaryHeight: summary.getBoundingClientRect().height,
                        lineHeight: parseFloat(getComputedStyle(summary).lineHeight),
                        summaryOverflow: getComputedStyle(summary).overflow,
                        ipEllipsis: ip ? getComputedStyle(ip).textOverflow : null,
                        rowHeight: row.getBoundingClientRect().height
                    };
                }""")
                assert bounds["table"] <= max(1100, bounds["container"]) + 2, bounds
                assert bounds["summaryHeight"] <= bounds["lineHeight"] * 2 + 1, bounds
                assert bounds["summaryOverflow"] == "hidden", bounds
                assert bounds["ipEllipsis"] == "ellipsis", bounds
                assert bounds["rowHeight"] <= 80, bounds
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            if width in [1440, 390]:
                await page.wait_for_function("!document.querySelector('#toast')?.classList.contains('show')")
                await page.locator("#main").screenshot(path=str(tmp_path / ("audit-overflow-" + str(width) + ".png")))
        row = page.locator('tr[data-row-id="legacy-lease-overflow"]')
        await row.get_by_role("button", name="详情", exact=True).click()
        detail = page.get_by_role("dialog", name="审计详情", exact=True)
        await expect(detail).to_contain_text("historical-instance-" * 20)
        await expect(detail).to_contain_text(long_ip)
        await expect(detail).to_contain_text(long_name)
        await browser.close()
