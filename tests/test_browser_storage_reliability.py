"""Closing a live bulk-mode dialog must leave its background job and storage usable."""
import asyncio
import os
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, expect
from test_browser_profile_settings import login

pytest_plugins = ("test_browser_profile_settings",)


async def test_close_bulk_mode_dialog_before_completion(running_gateway, monkeypatch):
    app, web, url, _, _ = running_gateway

    class Connection:
        async def discover(self):
            return {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}],
                    "resources": [], "prompts": [], "templates": []}

    @asynccontextmanager
    async def connector(spec):
        yield Connection()

    app.state.runtime.connector = connector
    rows = [await app.state.catalog.create({
        "name": f"Bulk close fixture {number}", "config": {"command": "fixture"},
    }, discover=False) for number in range(4)]
    entered, resume = asyncio.Event(), asyncio.Event()
    original = app.state.catalog.update

    async def paused(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.state.catalog, "update", paused)
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        try:
            page = await browser.new_page()
            errors, cancellations = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: cancellations.append(request.url) if request.url.endswith("/cancel") else None)
            await login(page, url)
            for label, mode in (("设为常驻", "eager"), ("设为按需", "lazy")):
                entered.clear()
                resume.clear()
                await page.goto(url + "/#/mcps")
                await page.get_by_label("选择当前页", exact=True).check()
                await page.get_by_role("button", name=label, exact=True).click()
                async with page.expect_response(lambda response: response.url.endswith("/api/v1/mcps/batch")
                                                and response.request.method == "POST") as pending:
                    await page.get_by_role("button", name="确认执行", exact=True).click()
                response = await pending.value
                assert response.status == 200
                job = await response.json()
                await asyncio.wait_for(entered.wait(), 10)
                popup = page.get_by_role("dialog", name="后台任务", exact=True)
                await expect(popup).to_contain_text("关闭窗口仅停止状态观察")
                await popup.get_by_role("button", name="关闭", exact=True).click()
                await expect(popup).to_have_count(0)
                future = app.state.jobs.tasks[job["id"]]
                resume.set()
                await asyncio.wait_for(asyncio.shield(future), 60)
                status = await app.state.jobs.store.get_status(job["id"])
                assert status["status"] == "completed" and status["succeeded"] == len(rows), status
                assert [(await app.state.catalog.get(row.id)).mode for row in rows] == [mode] * len(rows)
            assert not cancellations
            assert (await web.post(f"/api/v1/mcps/{rows[0].id}/refresh")).status_code == 200
            for name, heading in (("dashboard", "仪表盘"), ("tokens", "访问令牌")):
                async with page.expect_response(lambda response, name=name:
                                                response.url.split("?")[0].endswith("/api/v1/" + name)) as pending:
                    await page.goto(url + "/#/" + name)
                assert (await pending.value).status == 200
                await expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
                await expect(page.get_by_text("Internal Server Error", exact=False)).to_have_count(0)
            assert (await app.state.logs.audits.status())["write_failures"] == 0
            assert errors == []
        finally:
            resume.set()
            await browser.close()


async def test_audit_more_actions_have_visible_close_and_mobile_light_dismiss(running_gateway):
    _app, _web, url, _, _ = running_gateway
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        try:
            page = await browser.new_page()
            await login(page, url)
            for width in (1440, 390, 320):
                await page.set_viewport_size({"width": width, "height": 844})
                await page.goto(url + "/#/audit-logs")
                await expect(page.get_by_role("heading", name="审计日志", exact=True)).to_be_visible()
                more = page.get_by_role("button", name="更多操作", exact=True)
                await more.click()
                popup = page.get_by_role("dialog", name="审计日志操作", exact=True)
                close = popup.get_by_role("button", name="关闭", exact=True)
                await expect(close).to_be_visible()
                bounds = await close.bounding_box()
                assert bounds and 0 <= bounds["x"] < width and 0 <= bounds["y"] < 844
                await close.click()
                await expect(popup).to_have_count(0)
                await more.click()
                # Outside tap dismisses without requiring access to the covered trigger.
                await page.mouse.click(2, 2)
                await expect(popup).to_have_count(0)
                await more.click()
                await popup.get_by_role("button", name="查看日志状态", exact=True).click()
                await expect(popup).to_have_count(0)
                status = page.get_by_role("dialog", name="审计日志状态", exact=True)
                await expect(status).to_be_visible()
                await status.get_by_role("button", name="关闭", exact=True).click()
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        finally:
            await browser.close()
