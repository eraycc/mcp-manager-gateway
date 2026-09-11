"""Frontend workflows against a temporary gateway and real installed Chrome."""
from pathlib import Path
import json

import httpx
import pytest
from playwright.async_api import async_playwright, expect
from test_gateway import running_gateway  # noqa: F401


@pytest.mark.asyncio
async def test_frontend_workflows_and_mobile(running_gateway):  # noqa: F811
    app, web, url, _token, _row = running_gateway
    shots = Path(__file__).parents[1] / "artifacts/qa"
    shots.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe")
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.get_by_label("用户名", exact=True).fill("admin")
        await page.get_by_label("密码", exact=True).fill("password12345")
        await page.get_by_role("button", name="登录", exact=True).click()
        await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()

        await page.goto(url + "/#/mcps")
        await page.get_by_role("button", name="新增服务", exact=True).click()
        await page.get_by_role("button", name="JSON 配置", exact=True).click()
        config = {"name": "Browser REST", "slug": "browser-rest", "transport": "rest", "mode": "lazy",
                  "isolation": "service", "config": {"custom_setting": {"retain": True}, "tools": [
                      {"name": f"tool_{i:02}", "description": f"工具说明 {i}", "inputSchema": {"type": "object", "properties": {}},
                       "request": {"method": "GET", "url": "https://example.com/items"},
                       "response": {"type": "json"}} for i in range(12)]}}
        await page.get_by_label("完整配置 JSON").fill(json.dumps(config))
        await page.get_by_role("button", name="表单配置", exact=True).click()
        await page.get_by_role("button", name="JSON 配置", exact=True).click()
        assert json.loads(await page.get_by_label("完整配置 JSON").input_value())["config"]["custom_setting"] == {"retain": True}
        await page.get_by_role("button", name="创建服务", exact=True).click()
        await expect(page.get_by_text("Browser REST", exact=True)).to_be_visible()
        servers = (await web.get("/api/v1/mcps")).json()["items"]
        rest = next(x for x in servers if x["slug"] == "browser-rest")
        assert (await web.post("/api/v1/mcps/" + rest["id"] + "/refresh")).status_code == 200

        await page.get_by_role("button", name="导入配置", exact=True).click()
        await page.get_by_label("原始配置", exact=False).fill(json.dumps({"mcpServers": {"browser-import": {
            "command": "unused-diagnostic-command", "args": [], "mode": "disabled"}}}))
        await page.get_by_role("button", name="解析配置", exact=True).click()
        await expect(page.locator(".import-entry")).to_contain_text("browser-import")
        await page.get_by_role("button", name="确认导入", exact=True).click()
        await page.get_by_role("button", name="执行导入", exact=True).click()
        await expect(page.get_by_role("heading", name="导入结果", exact=True)).to_be_visible()
        await page.get_by_role("dialog", name="导入结果", exact=True).get_by_role("button", name="关闭").click()
        await expect(page.get_by_text("browser-import", exact=True).first).to_be_visible()

        await page.goto(url + "/#/tokens")
        await page.get_by_role("button", name="创建令牌", exact=True).click()
        await page.get_by_label("令牌名称", exact=True).fill("Browser token")
        await page.get_by_label("服务范围", exact=True).select_option("all")
        await page.get_by_role("button", name="保存", exact=True).click()
        await expect(page.get_by_role("heading", name="请立即保存访问令牌", exact=True)).to_be_visible()
        assert (await page.locator(".secret").inner_text()).startswith("mcpm_")
        await page.get_by_role("dialog").get_by_role("button", name="关闭").click()
        await expect(page.get_by_role("row").filter(has_text="Browser token")).to_be_visible()
        await expect(page.get_by_role("columnheader", name="调用次数", exact=True)).to_be_visible()
        assert "token" not in (await web.get("/api/v1/tokens")).json()["items"][0]

        await page.goto(url + "/#/users")
        await page.get_by_role("button", name="创建用户", exact=True).click()
        await page.get_by_label("用户名", exact=True).fill("browser-user")
        await page.get_by_label("密码", exact=True).fill("browser-password123")
        await page.get_by_label("服务范围", exact=True).select_option("all")
        await page.get_by_role("button", name="保存", exact=True).click()
        await expect(page.get_by_role("row").filter(has_text="browser-user")).to_be_visible()

        await page.goto(url + "/#/settings")
        await expect(page.get_by_role("tab", name="基本设置", exact=True)).to_have_attribute("aria-selected", "true")
        await page.get_by_label("登录有效期（天，0 为永不过期）").fill("0")
        await page.get_by_role("tab", name="运行与日志", exact=True).click()
        await expect(page.get_by_label("默认空闲回收时间（秒）")).to_be_visible()
        await expect(page.get_by_label("站点名称")).not_to_be_visible()
        await page.get_by_role("tab", name="匿名访问", exact=True).click()
        await expect(page.get_by_role("heading", name="匿名访问范围")).to_be_visible()
        await page.get_by_role("button", name="保存设置", exact=True).click()
        await expect(page.locator("#toast")).to_have_text("设置已保存")
        assert (await web.get("/api/v1/settings")).json()["jwt_days"] == 0
        await page.evaluate('scrollTo(0, 0)')
        await expect(page.locator('#toast')).not_to_be_visible()
        await page.screenshot(path=str(shots / "settings-tabs-desktop.png"), full_page=True)

        await page.set_viewport_size({"width": 390, "height": 844})
        await page.goto(url + "/#/dashboard")
        await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        boxes = await page.locator(".topbar button").evaluate_all("(nodes)=>nodes.filter(n=>getComputedStyle(n).display!=='none').map(n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,right:r.right,bottom:r.bottom}})")
        for box in boxes:
            assert 0 <= box["x"] < box["right"] <= 390
        for i, a in enumerate(boxes):
            for b in boxes[i+1:]:
                assert a["right"] <= b["x"] or b["right"] <= a["x"] or a["bottom"] <= b["y"] or b["bottom"] <= a["y"]
        await page.screenshot(path=str(shots / "dashboard-mobile-390.png"), full_page=True)

        # Seed only this fixture's database, then exercise a normal account's own lists.
        for i in range(11):
            response = await web.post("/api/v1/mcps", json={"name": f"Extra {i:02}", "slug": f"extra-{i}",
                "transport": "stdio", "mode": "lazy", "config": {"command": "unused"}})
            assert response.status_code == 200
        async with httpx.AsyncClient(base_url=url, trust_env=False) as user_web:
            for _ in range(11):
                assert (await user_web.post("/api/v1/auth/login", json={"username": "browser-user", "password": "browser-password123"})).status_code == 200

        await page.get_by_role("button", name="退出", exact=True).click()
        await page.get_by_label("用户名", exact=True).fill("browser-user")
        await page.get_by_label("密码", exact=True).fill("browser-password123")
        await page.get_by_role("button", name="登录", exact=True).click()
        await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()
        await page.set_viewport_size({"width": 1440, "height": 1000})
        await page.goto(url + "/#/profile")
        await expect(page.get_by_label("搜索会话", exact=True)).to_be_visible()
        sessions = page.get_by_label("会话列表", exact=True)
        await sessions.get_by_role("button", name="下一页", exact=True).click()
        await expect(sessions.locator(".pagination")).to_contain_text("2 / 2")
        await sessions.get_by_label("会话跳转页码", exact=True).fill("1")
        await sessions.get_by_role("button", name="跳转", exact=True).click()
        await sessions.get_by_label("会话筛选", exact=True).select_option("current")
        await expect(sessions.locator(".pagination")).to_contain_text("共 1 条")
        services = page.get_by_label("服务列表", exact=True)
        await services.get_by_role("button", name="下一页", exact=True).click()
        await expect(services.locator(".pagination")).to_contain_text("2 / 2")
        await services.get_by_label("搜索服务", exact=True).fill("Browser REST")
        await services.get_by_role("button", name="搜索", exact=True).click()
        await expect(services.locator(".pagination")).to_contain_text("共 1 条")
        await services.get_by_role("button", name="查看工具", exact=True).click()
        tools = page.get_by_label("工具列表", exact=True)
        await tools.get_by_role("button", name="下一页", exact=True).click()
        await expect(tools.locator(".pagination")).to_contain_text("2 / 2")
        await tools.get_by_label("搜索工具", exact=True).fill("tool_11")
        await tools.get_by_role("button", name="搜索", exact=True).click()
        await expect(tools.locator(".pagination")).to_contain_text("共 1 条")
        await expect(page.get_by_role("button", name="调用测试", exact=True)).to_have_count(0)
        for category in ["资源", "提示词", "资源模板"]:
            await page.get_by_role("dialog").get_by_role("button", name=category, exact=True).click()
            await expect(page.get_by_label("搜索" + category, exact=True)).to_be_visible()
            await expect(page.get_by_label(category + "筛选", exact=True)).to_be_visible()
        await page.get_by_role("dialog").get_by_role("button", name="关闭").click()
        await page.get_by_role("tab", name="Codex / 客户端配置", exact=True).click()
        await expect(page.get_by_text("bearer_token_env_var", exact=False)).to_be_visible()
        await page.evaluate("document.activeElement.blur(); scrollTo(0, 0)")
        await page.wait_for_function("scrollY === 0")
        await page.screenshot(path=str(shots / "profile-normal-desktop.png"), full_page=True)
        assert errors == []
        await browser.close()
