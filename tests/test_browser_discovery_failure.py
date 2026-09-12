"""Browser must replace stale success indicators after discovery fails."""
from playwright.async_api import expect

from test_browser_bug1 import bug1_browser  # noqa: F401


async def test_failed_saved_service_shows_warning(bug1_browser):
    page, url = bug1_browser
    await page.goto(url + "/#/mcps")
    await page.get_by_role("button", name="新增服务", exact=True).click()
    await page.get_by_label("服务名称", exact=True).fill("Broken")
    await page.get_by_label("可执行命令", exact=False).fill("missing")
    await page.route("**/api/v1/mcps", lambda r: r.fulfill(json={
        "id": "broken", "mode": "disabled", "cache_status": "error",
        "auto_disabled": True, "cache_error": "missing executable", "cache_error_code": "startup_failed"}))
    await page.get_by_role("button", name="创建服务", exact=True).click()
    await expect(page.locator("#toast")).to_contain_text("已自动禁用")
    await expect(page.locator("#toast")).to_have_attribute("data-status", "warning")


async def test_refresh_failure_replaces_open_cached_tool_list(bug1_browser):
    page, url = bug1_browser
    stale = {"tools": [{"name": "old_tool", "inputSchema": {"type": "object"}}],
             "cache_status": "ready", "cache_at": "2026-09-12T00:00:00Z"}
    failure = {"tools": [], "cache_status": "error", "auto_disabled": True,
               "cache_error": "upstream offline", "cache_error_code": "connection_error"}
    state = {"failed": False}
    await page.route("**/api/v1/mcps/broken/tools",
                     lambda r: r.fulfill(json=failure if state["failed"] else stale))
    async def fail(route):
        state["failed"] = True
        await route.fulfill(status=502, json={"detail": "upstream offline", "code": "connection_error"})
    await page.route("**/api/v1/mcps/broken/refresh", fail)
    await page.evaluate("async()=>{const m=await import('/mcps.js');await m.mcpTools({id:'broken',name:'Broken'},true)}")
    dialog = page.get_by_role("dialog")
    await expect(dialog).to_contain_text("old_tool")
    await dialog.get_by_role("button", name="刷新发现缓存").click()
    await expect(dialog).not_to_contain_text("old_tool")
    await expect(dialog).to_contain_text("已自动禁用")
    await expect(dialog).to_contain_text("upstream offline")
    await expect(page.locator("#toast")).to_have_attribute("data-status", "fail")


async def test_personal_oauth_recheck_and_explicit_recovery(bug1_browser):
    page, url = bug1_browser
    state = {"authorized": False, "starts": 0}
    await page.route("**/api/v1/mcps/personal/oauth/status",
        lambda r: r.fulfill(json={"scope": "user", "authorized": state["authorized"]}))
    await page.route("**/api/v1/mcps/personal/oauth/start",
        lambda r: r.fulfill(json={"authorization_url": "https://auth.test/authorize"}))
    async def start(route):
        state["starts"] += 1
        await route.fulfill(json={"ok": True})
    await page.route("**/api/v1/mcps/personal/start", start)
    await page.evaluate("async()=>{const m=await import('/mcps.js');await m.oauthDialog({id:'personal'},true)}")
    await expect(page.get_by_role("dialog")).to_contain_text("其他用户的个人授权不可复用")
    state["authorized"] = True
    await page.get_by_role("button", name="重新检查授权状态").click()
    await expect(page.get_by_role("dialog")).to_contain_text("当前登录用户已完成")
    await page.get_by_role("button", name="验证并启动").click()
    await expect(page.locator("#toast")).to_contain_text("验证成功")
    assert state["starts"] == 1
