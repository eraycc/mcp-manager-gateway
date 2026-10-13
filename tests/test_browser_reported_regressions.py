"""Focused browser regressions for dialog layout, full results and exports."""
import pytest
from playwright.async_api import async_playwright, expect
from test_gateway import running_gateway  # noqa: F401


@pytest.mark.asyncio
async def test_reported_console_regressions(running_gateway):  # noqa: F811
    app, web, url, _token, row = running_gateway
    await web.post(f'/api/v1/mcps/{row["id"]}/refresh')
    identity = await app.state.logs.append({
        "user_id": (await web.get("/api/v1/me")).json()["id"],
        "mcp_id": row["id"], "tool_name": "echo", "status": "success", "result": {"value": "ok"},
    })
    long_name = "very_long_tool_name_" * 30
    large_value = "完整结果" * 30000 + "END_OF_COMPLETE_RESULT"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe")
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.get_by_label("用户名", exact=True).fill("admin")
        await page.get_by_label("密码", exact=True).fill("password12345")
        await page.get_by_role("button", name="登录", exact=True).click()
        await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()
        await page.goto(url + "/#/logs")
        await page.locator(f'tr[data-row-id="{identity}"]').get_by_role("button", name="详情", exact=True).click()
        popup = page.get_by_role("dialog", name="调用详情", exact=True)
        await expect(popup.locator("pre")).to_contain_text('"status": "success"')
        assert not await popup.evaluate("(n)=>Array.from(n.querySelector('.dialog-body > .stack').childNodes).some(c=>c.nodeType===3 && c.textContent==='null')")
        await popup.get_by_role("button", name="关闭", exact=True).click()

        await page.goto(url + "/#/mcps")
        service = page.locator(f'tr[data-row-id="{row["id"]}"]')
        await service.get_by_role("checkbox").check()
        async with page.expect_download() as download:
            await page.get_by_role("button", name="导出", exact=True).click()
        assert (await download.value).suggested_filename == "mcp-services.json"
        await expect(service).to_be_visible()
        await expect(page.get_by_text("Failed to fetch", exact=True)).to_have_count(0)

        await page.route("**/api/v1/mcps/*/capabilities/tools/*", lambda route: route.fulfill(json={
            "name": long_name, "inputSchema": {"type": "object", "description": large_value},
        }))
        await page.route("**/api/v1/mcps/*/test", lambda route: route.fulfill(json={
            "content": [{"type": "text", "text": large_value}],
        }))
        await service.get_by_role("button", name="工具 / 测试", exact=True).click()
        directory = page.get_by_role("dialog", name="Echo · 能力目录", exact=True)
        await expect(directory.locator(".capability-card")).to_have_count(2)
        await directory.get_by_role("button", name="查看完整定义", exact=True).first.click()
        await expect(directory.locator(".capability-definition pre")).to_contain_text("END_OF_COMPLETE_RESULT")
        await directory.get_by_role("button", name="调用测试", exact=True).first.click()
        tool = page.get_by_role("dialog", name="测试 · " + long_name, exact=True)
        assert await tool.evaluate("(n)=>n.scrollWidth<=n.clientWidth+1 && n.querySelector('.dialog-body').scrollWidth<=n.querySelector('.dialog-body').clientWidth+1")
        assert (await tool.locator(".dialog-head button").bounding_box())["width"] >= 50
        await tool.get_by_role("button", name="执行工具", exact=True).click()
        await expect(tool.locator("pre")).to_contain_text("END_OF_COMPLETE_RESULT")
        await expect(page.get_by_text("结果较大，界面仅显示有限预览", exact=True)).to_have_count(0)
        await tool.locator(".dialog-head").get_by_role("button", name="关闭", exact=True).click()

        responses = []
        await page.route("**/api/v1/mcps/*/capabilities?*", lambda route: route.fulfill(json={
            "items": ([{"name": "after-refresh"}] if responses else []),
            "total": 1 if responses else 0, "generation": "new" if responses else "",
            "next_cursor": None, "refresh_required": not responses,
        }))
        await page.route("**/api/v1/mcps/*/refresh", lambda route: (responses.append("refreshed"), route.fulfill(json={"status": "ready", "tool_count": 1}))[1])
        await directory.locator(".dialog-head").get_by_role("button", name="关闭", exact=True).click()
        await service.get_by_role("button", name="工具 / 测试", exact=True).click()
        directory = page.get_by_role("dialog", name="Echo · 能力目录", exact=True)
        await directory.get_by_role("button", name="刷新发现缓存", exact=True).click()
        await expect(directory.locator(".capability-card")).to_contain_text("after-refresh")
        assert errors == []
        await browser.close()
