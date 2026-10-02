"""Real browser regression for navigation, stable polling and import state."""
import asyncio
import json
from pathlib import Path
import pytest
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from playwright.async_api import async_playwright, expect
from test_gateway import running_gateway  # noqa: F401

@pytest.mark.asyncio
async def test_navigation_protocol_and_import_wizard(running_gateway, monkeypatch):  # noqa: F811
    app, web, url, token, row = running_gateway
    active = peak = 0
    discover = app.state.runtime.discover
    async def counted(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.1)
            return await discover(*args, **kwargs)
        finally:
            active -= 1
    monkeypatch.setattr(app.state.runtime, "discover", counted)
    async def sources(_channel):
        return {"sources": [{"path": "/temporary/client.json", "content": json.dumps({"mcpServers": {
            "from-scan": {"command": "unused", "mode": "disabled"}}})}], "errors": []}
    monkeypatch.setattr("mcp_manager.catalog_api.scan_sources", sources)
    shots = Path(__file__).parents[1] / "artifacts/qa"
    shots.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe")
        page = await browser.new_page(viewport={"width": 1440, "height": 1000}, has_touch=True)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(url)
        await page.wait_for_timeout(500)
        assert errors == [], errors
        assert await page.locator("#app").inner_text(), await page.content()
        await page.get_by_label("用户名", exact=True).fill("admin")
        await page.get_by_label("密码", exact=True).fill("password12345")
        await page.get_by_role("button", name="登录", exact=True).click()
        await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()
        await page.evaluate("window.dashboardMain=document.querySelector('main')")
        async with httpx2.AsyncClient(headers={"Authorization":"Bearer "+token},trust_env=False) as http:
            async with Client(streamable_http_client(url+"/mcp",http_client=http),cache=None) as client:
                await client.call_tool("echo__echo",{"value":"dashboard-live"})
        await expect(page.locator(".stat strong").first).to_have_text("1",timeout=15000)
        await expect(page.locator("[data-dashboard-recent] tbody")).to_contain_text("echo")
        assert await page.evaluate("dashboardMain===document.querySelector('main')")
        await expect(page.locator(".stat strong").first).to_have_text("1")
        await page.get_by_role("button", name="折叠导航", exact=True).click()
        await expect(page.locator("body")).to_have_class("sidebar-collapsed")
        current_url=page.url
        await page.get_by_role("link",name="访问令牌",exact=True).tap()
        assert page.url == current_url
        await expect(page.get_by_role("tooltip")).to_have_text("访问令牌")
        await page.get_by_role("link",name="访问令牌",exact=True).tap()
        await expect(page.get_by_role("heading",name="访问令牌",exact=True)).to_be_visible()
        await page.get_by_role("link", name="MCP 服务", exact=True).hover()
        await expect(page.get_by_role("tooltip")).to_have_text("MCP 服务")
        await page.get_by_role("link", name="MCP 服务", exact=True).click()
        await expect(page.get_by_role("heading", name="MCP 服务", exact=True)).to_be_visible()
        # Polling must preserve live form nodes, unsubmitted text, selection and focus.
        await page.get_by_label("搜索", exact=True).fill("unsubmitted search")
        await page.get_by_label("选择当前页", exact=True).check()
        await page.get_by_label("搜索", exact=True).focus()
        await page.evaluate("window.savedSearch=document.querySelector('input[type=search]');window.savedMain=document.querySelector('main')")
        await web.patch("/api/v1/mcps/" + row["id"], json={"description": "poll-updated-description"})
        await expect(page.get_by_text("poll-updated-description", exact=True)).to_be_visible(timeout=15000)
        assert await page.evaluate("savedSearch===document.querySelector('input[type=search]') && savedMain===document.querySelector('main') && document.activeElement===savedSearch")
        await expect(page.get_by_label("搜索", exact=True)).to_have_value("unsubmitted search")
        await expect(page.get_by_label("选择当前页", exact=True)).to_be_checked()

        await page.get_by_role("button", name="新增服务", exact=True).click()
        await page.get_by_label("命令参数", exact=False).fill("[broken")
        await page.get_by_label("传输协议", exact=True).select_option("streamable-http")
        await expect(page.get_by_label("传输协议", exact=True)).to_have_value("stdio")
        await expect(page.locator("dialog .error")).to_contain_text("协议未切换")
        await page.get_by_label("命令参数", exact=False).fill('["keep-me"]')
        await page.get_by_label("传输协议", exact=True).select_option("streamable-http")
        await page.get_by_label("服务 URL", exact=False).fill("https://example.com/mcp")
        await page.get_by_label("认证方式", exact=True).select_option("bearer")
        await page.get_by_label("Bearer Token", exact=True).fill("disposable-token")
        await page.get_by_label("认证方式", exact=True).select_option("basic")
        await page.get_by_label("认证方式", exact=True).select_option("bearer")
        await expect(page.get_by_label("Bearer Token", exact=True)).to_have_value("disposable-token")
        await page.get_by_label("传输协议", exact=True).select_option("sse")
        await expect(page.get_by_label("服务 URL", exact=False)).to_be_visible()
        await page.get_by_label("传输协议", exact=True).select_option("rest")
        await expect(page.get_by_role("button", name="添加 REST 工具", exact=True)).to_be_visible()
        await page.get_by_label("传输协议", exact=True).select_option("stdio")
        await expect(page.get_by_label("命令参数", exact=False)).to_have_value('[\n  "keep-me"\n]')
        await page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
        assert (await web.get("/api/v1/mcps")).json()["total"] == 1

        await page.get_by_role("button", name="导入配置", exact=True).click()
        raw = page.get_by_label("原始配置", exact=False)
        samples = {
            "generic": json.dumps({"mcpServers": {"generic-one": {"command": "unused", "mode": "disabled"}}}),
            "codex": '[mcp_servers.codex_one]\ncommand = "unused"\nenabled = false',
            "claude": json.dumps({"mcpServers": {"claude-one": {"command": "unused", "disabled": True}}}),
            "dsh": json.dumps({"entries": [{"name": "dsh-one", "command": "unused", "disabled": True}]})
        }
        for expected_count, (channel, source) in enumerate(samples.items(), 1):
            await page.get_by_label("配置来源", exact=True).select_option(channel)
            await raw.fill(source)
            await page.get_by_role("button", name="解析配置", exact=True).click()
            await expect(page.locator(".import-entry")).to_have_count(expected_count)
        await expect(page.locator(".import-entry")).to_have_count(4)
        await page.get_by_label("配置来源", exact=True).select_option("generic")
        await page.get_by_role("button", name="扫描配置位置", exact=True).click()
        await page.get_by_role("button", name="解析此文件并追加", exact=True).click()
        await expect(page.locator(".import-entry")).to_have_count(5)
        await page.get_by_label("上传配置文件", exact=True).set_input_files({"name": "upload.json", "mimeType": "application/json", "buffer": samples["generic"].encode()})
        await expect(raw).to_have_value(samples["generic"])
        await page.get_by_role("button", name="解析配置", exact=True).click()
        await expect(page.locator(".import-entry")).to_have_count(6)
        await page.get_by_text("统一 JSON 编辑", exact=True).click()
        unified=page.get_by_label("待导入服务 JSON", exact=False)
        pending=json.loads(await unified.input_value())
        pending[0]["description"]="pending change"
        await unified.fill(json.dumps(pending))
        await page.locator(".import-entry").first.get_by_role("button",name="移除",exact=True).click()
        await expect(page.locator("#toast")).to_contain_text("请先应用")
        await expect(page.locator(".import-entry")).to_have_count(6)
        await page.get_by_role("button",name="解析配置",exact=True).click()
        await expect(page.locator(".import-entry")).to_have_count(6)
        await page.get_by_role("button",name="去重",exact=True).click()
        await expect(page.locator(".import-entry")).to_have_count(6)
        await page.get_by_role("button",name="应用 JSON 修改",exact=True).click()
        await page.get_by_role("button", name="去重", exact=True).click()
        await page.get_by_role("dialog", name="去重结果", exact=True).get_by_role("button", name="关闭").click()
        assert await page.locator(".import-entry").count() < 6
        while await page.locator(".import-entry").count():
            await page.locator(".import-entry").first.get_by_role("button", name="移除").click()
        await expect(page.locator(".import-entry")).to_have_count(0)
        tools = [{"name": "read", "inputSchema": {"type": "object"}, "request": {"method": "GET", "url": "https://example.com"}, "response": {"type": "json"}}]
        batch = [{"name": f"rest-{i}", "slug": f"rest-{i}", "transport": "rest", "mode": "lazy", "config": {"tools": tools, "custom": i}} for i in range(5)]
        await page.get_by_label("待导入服务 JSON", exact=False).fill(json.dumps(batch))
        await page.get_by_role("button", name="应用 JSON 修改", exact=True).click()
        await page.get_by_role("button", name="诊断全部服务", exact=True).click()
        await expect(page.locator(".import-entry").filter(has_text="诊断成功")).to_have_count(5)
        assert 1 < peak <= 3
        await page.locator(".import-entry").first.get_by_role("button", name="预览工具与能力").click()
        await expect(page.get_by_role("dialog").last.locator("pre")).to_contain_text("read")
        await page.get_by_role("dialog").last.get_by_role("button", name="关闭").click()
        await page.locator(".import-entry").first.get_by_role("button", name="编辑配置").click()
        item = dict(batch[0], description="edited invalidates diagnosis")
        await page.get_by_label("服务配置 JSON", exact=False).fill(json.dumps(item))
        await page.get_by_role("dialog").last.get_by_role("button", name="保存", exact=True).click()
        await expect(page.locator(".import-entry").first).to_contain_text("未诊断")
        await page.locator(".import-entry").first.get_by_role("button", name="诊断 / 发现工具").click()
        await expect(page.locator(".import-entry").first).to_contain_text("诊断成功")
        assert (await web.get("/api/v1/mcps")).json()["total"] == 1
        await page.screenshot(path=str(shots / "import-wizard.png"), full_page=True)
        await page.get_by_label("名称冲突处理", exact=True).select_option("copy")
        await page.get_by_role("button", name="确认导入", exact=True).click()
        await page.get_by_role("button", name="执行导入", exact=True).click()
        await page.get_by_role("dialog", name="导入结果", exact=True).get_by_role("button", name="关闭").click()
        assert (await web.get("/api/v1/mcps")).json()["total"] == 6

        await page.locator('summary[aria-label="显示模式"]').click()
        await page.locator(".action-menu-panel").filter(has_text="显示模式").get_by_role(
            "button", name="深色", exact=True
        ).click()
        await expect(page.locator("body")).to_have_class(__import__("re").compile("dark"))
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.get_by_role("button", name="打开导航", exact=True).click()
        await page.locator(".collapse-toggle").click()
        await expect(page.locator(".sidebar")).to_have_attribute("aria-modal", "true")
        assert await page.evaluate("document.querySelector('.sidebar').contains(document.activeElement)")
        await page.keyboard.press("Escape")
        await expect(page.get_by_role("button", name="打开导航", exact=True)).to_be_focused()
        await page.get_by_role("button", name="打开导航", exact=True).click()
        await page.locator(".collapse-toggle").click()
        await page.locator(".drawer-close").click()
        await page.get_by_role("button", name="打开导航", exact=True).click()
        await page.locator(".collapse-toggle").click()
        await page.locator(".drawer-backdrop").click(position={"x": 370, "y": 500})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert await page.evaluate("getComputedStyle(document.documentElement).backgroundColor===getComputedStyle(document.querySelector('.shell')).backgroundColor")
        await page.get_by_role("button", name="打开导航", exact=True).click()
        await page.locator(".collapse-toggle").click()
        await page.screenshot(path=str(shots / "mobile-dark-drawer.png"), full_page=False)
        await page.set_viewport_size({"width":390,"height":400})
        assert await page.locator(".sidebar-nav").evaluate("(n)=>n.scrollHeight>n.clientHeight")
        await page.get_by_role("link", name="系统设置",exact=True).scroll_into_view_if_needed()
        await expect(page.get_by_role("link", name="系统设置",exact=True)).to_be_visible()
        await page.keyboard.press("Escape")
        assert errors == []
        await browser.close()


@pytest.mark.asyncio
async def test_import_stale_responses_and_partial_retry(running_gateway):  # noqa: F811
    _, _web, url, _, _row = running_gateway
    item=lambda name: {"name":name,"slug":name,"transport":"stdio","mode":"disabled","config":{"command":"unused"}}
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True,executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe")
        page=await browser.new_page(viewport={"width":1440,"height":1000})
        await page.goto(url)
        await page.get_by_label("用户名",exact=True).fill("admin")
        await page.get_by_label("密码",exact=True).fill("password12345")
        await page.get_by_role("button",name="登录",exact=True).click()
        await expect(page.get_by_role("heading",name="仪表盘",exact=True)).to_be_visible()
        await page.goto(url+"/#/mcps")
        await page.get_by_role("button",name="新增服务",exact=True).click()
        await page.get_by_role("button",name="JSON 配置",exact=True).click()
        await page.get_by_label("完整配置 JSON",exact=False).fill(json.dumps({"name":"oauth-string","transport":"streamable-http","mode":"lazy","isolation":"user","config":{"url":"https://example.com/mcp","auth":{"type":"oauth","scopes":"read write"}}}))
        await page.get_by_role("button",name="表单配置",exact=True).click()
        await expect(page.get_by_label("Scopes（空格分隔）",exact=True)).to_have_value("read write")
        await page.get_by_role("dialog").get_by_role("button",name="关闭",exact=True).click()
        await page.get_by_role("button",name="导入配置",exact=True).click()
        await page.get_by_text("统一 JSON 编辑",exact=True).click()
        unified=page.get_by_label("待导入服务 JSON",exact=False)
        async def apply(items):
            await unified.fill(json.dumps(items))
            await page.get_by_role("button",name="应用 JSON 修改",exact=True).click()
        await apply([item("original")])
        started,release=asyncio.Event(),asyncio.Event()
        async def delayed_parse(route):
            response=await route.fetch()
            started.set()
            await release.wait()
            await route.fulfill(response=response)
        await page.route("**/api/v1/mcps/import-preview",delayed_parse)
        await page.get_by_label("原始配置",exact=False).fill(json.dumps([item("stale-parse")]))
        await page.get_by_role("button",name="解析配置",exact=True).click()
        await asyncio.wait_for(started.wait(),5)
        await apply([item("new-json")])
        release.set()
        await expect(page.get_by_role("button",name="解析配置",exact=True)).to_be_enabled()
        await expect(page.locator(".import-entry")).to_have_count(1)
        await expect(page.locator(".import-entry")).to_contain_text("new-json")
        await page.unroute("**/api/v1/mcps/import-preview",delayed_parse)
        started,release=asyncio.Event(),asyncio.Event()
        async def delayed_dedup(route):
            response=await route.fetch()
            started.set()
            await release.wait()
            await route.fulfill(response=response)
        await page.route("**/api/v1/mcps/import-deduplicate",delayed_dedup)
        await page.get_by_role("button",name="去重",exact=True).click()
        await asyncio.wait_for(started.wait(),5)
        await apply([item("newer-json")])
        release.set()
        await expect(page.locator("#toast")).to_contain_text("忽略旧操作结果")
        await expect(page.locator(".import-entry")).to_contain_text("newer-json")
        await page.unroute("**/api/v1/mcps/import-deduplicate",delayed_dedup)
        started,release=asyncio.Event(),asyncio.Event()
        async def delayed_scan(route):
            started.set()
            await release.wait()
            await route.fulfill(json={"sources":[{"path":"/old-channel.json","content":"old-channel-source"}],"errors":[]})
        await page.route("**/api/v1/mcps/import-sources?*",delayed_scan)
        await page.get_by_role("button",name="扫描配置位置",exact=True).click()
        await asyncio.wait_for(started.wait(),5)
        await page.get_by_label("配置来源",exact=True).select_option("codex")
        release.set()
        await page.wait_for_timeout(200)
        await expect(page.get_by_text("/old-channel.json",exact=True)).to_have_count(0)
        assert await page.get_by_label("原始配置",exact=False).input_value() != "old-channel-source"
        await page.unroute("**/api/v1/mcps/import-sources?*",delayed_scan)
        await apply([item("saved"),item("retry-me")])
        async def partial(route):
            assert route.request.post_data_json["refresh"] is True
            await route.fulfill(json={"results":[{"name":"saved","status":"imported"}],"errors":[{"name":"retry-me","error":"temporary failure"}]})
        await page.route("**/api/v1/mcps/import",partial)
        await page.get_by_role("button",name="确认导入",exact=True).click()
        await page.get_by_role("button",name="执行导入",exact=True).click()
        await page.get_by_role("dialog",name="部分导入结果",exact=True).get_by_role("button",name="关闭",exact=True).click()
        await expect(page.get_by_role("dialog",name="导入 MCP 配置",exact=True)).to_be_visible()
        await expect(page.locator(".import-entry")).to_have_count(1)
        await expect(page.locator(".import-entry")).to_contain_text("retry-me")
        await expect(page.locator(".import-entry")).to_contain_text("temporary failure")
        await page.unroute("**/api/v1/mcps/import",partial)
        await page.get_by_role("button",name="确认导入",exact=True).click()
        await page.get_by_role("button",name="执行导入",exact=True).click()
        await expect(page.get_by_role("dialog",name="导入结果",exact=True)).to_be_visible()
        await browser.close()
