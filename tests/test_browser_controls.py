"""Sidebar, drag, authorization and job regressions in temporary Chrome."""
from pathlib import Path
import pytest
from playwright.async_api import async_playwright, expect
from test_gateway import running_gateway  # noqa: F401


@pytest.mark.asyncio
async def test_frontend_controls(running_gateway):  # noqa: F811
    _app, web, url, _token, _row = running_gateway
    shots = Path(__file__).parents[1] / "artifacts/qa"
    shots.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe")
        page = await browser.new_page(viewport={"width": 1280, "height": 800}, has_touch=True)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(url)
        await page.get_by_label("用户名", exact=True).fill("admin")
        await page.get_by_label("密码", exact=True).fill("password12345")
        await page.get_by_role("button", name="登录", exact=True).click()
        await expect(page.locator(".collapse-toggle")).to_contain_text("收起")
        await page.locator(".collapse-toggle").click()
        await expect(page.locator("body")).to_have_class(__import__("re").compile("sidebar-collapsed"))
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.locator(".mobile-menu").click()
        await expect(page.locator("body")).to_have_attribute("data-mobile-nav", "rail")
        await page.locator(".collapse-toggle").click()
        await expect(page.locator("body")).to_have_attribute("data-mobile-nav", "expanded")
        await page.locator(".collapse-toggle").click()
        await expect(page.locator("body")).to_have_attribute("data-mobile-nav", "rail")
        original_url = page.url
        await page.locator('a[href="#/tokens"]').tap()
        assert page.url == original_url
        await expect(page.get_by_role("tooltip")).to_have_text("访问令牌")
        await page.locator('a[href="#/tokens"]').tap()
        await expect(page.locator("body")).to_have_attribute("data-mobile-nav", "rail")
        assert not await page.locator(".shell").evaluate("(n)=>n.inert")
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        await page.screenshot(path=str(shots / "mobile-rail.png"))
        await page.emulate_media(reduced_motion="reduce")
        assert await page.locator(".sidebar").evaluate("(n)=>getComputedStyle(n).transitionDuration") == "0s"
        await page.emulate_media(reduced_motion="no-preference")
        await page.locator(".mobile-menu").click()
        await expect(page.locator("body")).to_have_attribute("data-mobile-nav", "hidden")
        await page.set_viewport_size({"width": 1280, "height": 800})
        await page.goto(url + "/#/tokens")
        await page.get_by_role("button", name="创建令牌", exact=True).click()
        await expect(page.get_by_label("启用资源工具", exact=True)).to_be_visible()
        await expect(page.get_by_label("允许 Agent 提议批量配置 MCP", exact=True)).to_be_visible()
        await expect(page.get_by_text("gateway_list_resources", exact=False)).to_be_visible()
        await expect(page.get_by_text("gateway_propose_mcp", exact=False)).to_be_visible()
        await page.get_by_label("令牌名称", exact=True).fill("Selected snapshot")
        await page.get_by_role("button", name="全选当前服务", exact=True).click()
        await expect(page.get_by_label("服务范围", exact=True)).to_have_value("selected")
        await expect(page.locator(".grant-count")).to_contain_text("已选 1")
        await page.locator(".grant-selector").get_by_role("button", name="清空选择", exact=True).click()
        await expect(page.locator(".grant-count")).to_contain_text("已选 0")
        await page.get_by_label("授权协议筛选", exact=True).select_option("rest")
        await expect(page.locator(".grant-list input")).to_have_count(0)
        await page.get_by_label("授权协议筛选", exact=True).select_option("stdio")
        await page.get_by_role("button", name="选择搜索匹配", exact=True).click()
        await expect(page.locator(".grant-count")).to_contain_text("已选 1")
        await page.get_by_label("搜索授权服务", exact=True).fill("no-match")
        await expect(page.locator(".grant-list input")).to_have_count(0)
        await page.get_by_role("button", name="保存", exact=True).click()
        await expect(page.get_by_role("dialog", name="访问令牌", exact=True)).to_be_visible()
        row = next(x for x in (await web.get("/api/v1/tokens")).json()["items"] if x["name"] == "Selected snapshot")
        assert row["scope_mode"] == "selected" and len(row["mcp_ids"]) == 1
        await page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
        await page.evaluate("""async()=>{const c=await import('/core.js'); await c.jobDialog({id:'terminal-test',status:'completed'});c.toast('失败提示',false)}""")
        await expect(page.get_by_role("button", name="取消后台任务", exact=True)).to_have_count(0)
        await expect(page.locator("#toast")).to_have_attribute("data-status", "fail")
        assert await page.locator("#toast").evaluate("(n)=>n.matches(':popover-open')")
        toast_box = await page.locator("#toast").bounding_box()
        assert toast_box["y"] < 50 and toast_box["x"] > 600
        await page.screenshot(path=str(shots / "job-toast-top-layer.png"))
        await page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).first.click()
        await page.evaluate("""()=>{const w=document.createElement('div');w.className='table-scroll';w.id='drag-probe';w.style='width:400px';const t=document.createElement('table');t.style='width:1200px';const tr=t.insertRow();tr.insertCell().textContent='Drag text';tr.insertCell().innerHTML='<button id="probe-button">Action</button>';w.append(t);document.querySelector('main').prepend(w)}""")
        box = await page.locator("#drag-probe").bounding_box()
        await page.mouse.move(box["x"] + 250, box["y"] + 20)
        await page.mouse.down()
        await page.mouse.move(box["x"] + 50, box["y"] + 20, steps=10)
        await page.mouse.up()
        assert await page.locator("#drag-probe").evaluate("(n)=>n.scrollLeft") > 100
        # Interactive table controls remain clickable without a drag.
        await page.locator("#probe-button").evaluate("(n)=>n.onclick=()=>window.probeClicked=true")
        await page.locator("#probe-button").click()
        assert await page.evaluate("window.probeClicked")
        await page.goto(url + "/#/mcps")
        await page.get_by_label("选择当前页", exact=True).check()
        await page.get_by_role("button", name="删除", exact=True).click()
        await page.get_by_role("button", name="确认执行", exact=True).click()
        await expect(page.get_by_role("heading", name="正在删除服务", exact=True)).to_be_visible()
        await expect(page.locator("#toast")).to_have_text("删除完成", timeout=15000)
        await expect(page.locator("dialog")).to_have_count(0)
        assert (await web.get("/api/v1/mcps")).json()["total"] == 0
        # Failed jobs keep details visible and never offer cancellation.
        await page.evaluate("""async()=>{const c=await import('/core.js');await c.jobDialog({id:'failed-test',status:'failed',error:'expected failure'},{autoCloseOnSuccess:true})}""")
        await expect(page.get_by_role("dialog")).to_contain_text("expected failure")
        await expect(page.get_by_role("button", name="取消后台任务", exact=True)).to_have_count(0)
        await page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).first.click()
        await page.goto(url + "/#/profile")
        await page.get_by_role("tab", name="Codex / 客户端配置", exact=True).click()
        await expect(page.get_by_text('"command": "mcp-manager"', exact=False)).to_be_visible()
        await page.get_by_role("tab", name="本地 stdio", exact=True).click()
        await expect(page.get_by_text("mcp-manager stdio --url", exact=False)).to_be_visible()
        assert "uv run" not in await page.locator("main").inner_text()
        assert errors == []
        await browser.close()
