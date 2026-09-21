"""OAuth, cache and cross-origin settings regressions on an isolated HTTP server."""
import asyncio
import json
import socket

import httpx
import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from mcp_manager.app import create_app
from mcp_manager.config import Settings


@pytest.fixture
async def bug1_browser(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = create_app(Settings(data_dir=tmp_path, database_url="", secret_key="browser-bug1", public_url=url))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(300):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.02)
        assert server.started
        async with httpx.AsyncClient(base_url=url, trust_env=False) as web:
            response = await web.post("/api/v1/auth/register", json={"username": "admin", "password": "password12345"})
            assert response.status_code == 200
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe")
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.goto(url)
            await page.get_by_label("用户名", exact=True).fill("admin")
            await page.get_by_label("密码", exact=True).fill("password12345")
            await page.get_by_role("button", name="登录", exact=True).click()
            await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()
            yield page, url
            await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()


@pytest.mark.asyncio
async def test_oauth_profile_only_oauth_and_disconnect(bug1_browser):
    page, url = bug1_browser
    rows = [
        {"id": "plain", "name": "普通服务", "slug": "plain", "transport": "stdio",
         "mode": "lazy", "isolation": "service", "revision": 1, "auth_type": "none"},
        {"id": "oauth", "name": "授权服务", "slug": "oauth", "transport": "streamable-http",
         "mode": "lazy", "isolation": "user", "revision": 1, "auth_type": "oauth"},
    ]
    await page.route("**/api/v1/mcps?*", lambda r: r.fulfill(json={"items": rows, "total": 2}))
    async def credential_status(route):
        oauth = "/mcps/oauth/" in route.request.url
        await route.fulfill(json={
            "required": oauth,
            "auth_type": "oauth" if oauth else "none",
            "isolation": "user" if oauth else "service",
            "config_isolation": "shared" if oauth else None,
            "state": "pending_authorization" if oauth else "not_required",
            "action": "authorize" if oauth else None,
        })
    await page.route("**/api/v1/mcps/*/credentials/status", credential_status)
    await page.route("**/api/v1/mcps/oauth/oauth/status", lambda r: r.fulfill(json={"authorized": True, "scope": "user"}))
    await page.route("**/api/v1/mcps/oauth/oauth/start", lambda r: r.fulfill(json={"authorization_url": "https://example.com/authorize"}))
    disconnected = []
    async def disconnect(route):
        disconnected.append(route.request.method)
        await route.fulfill(json={"ok": True})
    await page.route("**/api/v1/mcps/oauth/oauth/disconnect", disconnect)
    await page.goto(url + "/#/profile")
    await expect(page.get_by_role("button", name="授权", exact=True)).to_have_count(1)
    await page.get_by_role("button", name="授权", exact=True).click()
    dialog = page.get_by_role("dialog", name="授权服务", exact=True)
    button = dialog.get_by_role("button", name="断开 OAuth 授权", exact=True)
    await expect(button).to_have_class("danger")
    await expect(dialog.get_by_role("link", name="打开授权页面")).to_be_visible()
    await button.click()
    await page.get_by_role("button", name="确认执行", exact=True).click()
    await expect(button).to_have_count(0)
    assert disconnected == ["POST"]
    await page.route("**/api/v1/mcps/oauth/oauth/status", lambda r: r.fulfill(json={"authorized": False, "scope": "user"}))
    await page.get_by_role("button", name="授权", exact=True).click()
    await expect(page.get_by_role("button", name="断开 OAuth 授权", exact=True)).to_have_count(0)
    await expect(page.get_by_role("link", name="打开授权页面")).to_be_visible()
    await page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
    await page.route("**/api/v1/mcps/oauth/oauth/status", lambda r: r.fulfill(json={"authorized": True, "scope": "user"}))
    await page.evaluate("async()=>{const m=await import('/mcps.js');await m.oauthDialog({id:'oauth'},false)}")
    await expect(page.get_by_role("dialog")).to_contain_text("当前登录用户已完成 OAuth 授权")
    await expect(page.get_by_role("button", name="断开 OAuth 授权", exact=True)).to_have_count(1)
    await expect(page.get_by_role("link", name="打开授权页面")).to_have_count(1)


@pytest.mark.asyncio
async def test_cache_states_refresh_and_structured_error(bug1_browser):
    page, url = bug1_browser
    rows = [{"id": key, "name": key, "transport": "stdio", "mode": "lazy", "tool_count": 0,
             "cache_status": status, "cache_error": error, "cache_error_code": code}
            for key, status, error, code in [
                ("never", "empty", None, None),
                ("oauth", "auth_required", None, "auth_required"),
                ("failed", "error", "连接超时", "startup_timeout"),
            ]]
    await page.route("**/api/v1/mcps?*", lambda r: r.fulfill(json={"items": rows, "total": 3}))
    await page.goto(url + "/#/mcps")
    await expect(page.get_by_role("row").filter(has_text="never")).to_contain_text("从未刷新")
    await expect(page.get_by_role("row").filter(has_text="oauth")).to_contain_text("待授权")
    await expect(page.get_by_role("row").filter(has_text="failed")).to_contain_text("startup_timeout")
    await page.route("**/api/v1/mcps/never/tools", lambda r: r.fulfill(json={"tools": [], "cache_status": "empty"}))
    await page.route("**/api/v1/mcps/never/refresh", lambda r: r.fulfill(json={
        "tools": [{"name": "echo", "inputSchema": {"type": "object"}}],
        "cache_status": "ready", "cache_at": "2026-09-12T03:00:00Z"}))
    await page.get_by_role("row").filter(has_text="never").get_by_role("button", name="工具 / 测试").click()
    dialog = page.get_by_role("dialog")
    await expect(dialog).to_contain_text("从未刷新")
    await dialog.get_by_role("button", name="刷新发现缓存").click()
    await expect(dialog).not_to_contain_text("从未刷新")
    await expect(dialog).to_contain_text("2026")
    await page.route("**/api/v1/mcps/never/test", lambda r: r.fulfill(status=502, json={
        "detail": "Complete OAuth authorization in the Web console", "code": "auth_required"}))
    await dialog.get_by_role("button", name="调用测试").click()
    await page.get_by_role("button", name="执行工具").click()
    await expect(page.get_by_role("dialog", name="测试 · echo")).to_contain_text("auth_required")
    await expect(page.get_by_role("dialog", name="测试 · echo")).to_contain_text("请在 MCP 服务")


@pytest.mark.asyncio
async def test_cors_settings(bug1_browser):
    page, url = bug1_browser
    await page.goto(url + "/#/settings")
    cors = page.get_by_label("允许跨域接入的来源", exact=True)
    hosts = page.get_by_label("允许的监听 Host / IP 列表", exact=True)
    await expect(cors).to_have_value("*")
    await expect(hosts).to_have_value("*")
    await cors.fill("https://client.example.com\nhttp://localhost:3000")
    await hosts.fill("0.0.0.0\nmanager.example:8765")
    async with page.expect_response("**/api/v1/settings") as pending:
        await page.get_by_role("button", name="保存设置", exact=True).click()
    response = await pending.value
    assert response.status == 200
    payload = json.loads(response.request.post_data)
    assert payload["cors_origins"] == ["https://client.example.com", "http://localhost:3000"]
    assert payload["allowed_hosts"] == ["0.0.0.0", "manager.example:8765"]
    await expect(page.get_by_text("管理控制台仍只允许同源访问。", exact=False)).to_be_visible()


@pytest.mark.asyncio
async def test_auth_poll_stops(bug1_browser):
    page, url = bug1_browser
    attempts = []
    async def denied(route):
        attempts.append(True)
        await route.fulfill(status=401, json={"detail": "Not authenticated"})
    await page.route("**/api/v1/mcps?*", lambda r: r.fulfill(json={"items": [], "total": 0}))
    await page.clock.install()
    await page.goto(url + "/#/mcps")
    await expect(page.get_by_text("没有匹配记录，调整筛选条件或创建第一条记录。")).to_be_visible()
    await page.route("**/api/v1/mcps?*", denied)
    await page.clock.fast_forward(11000)
    await expect(page.get_by_label("密码", exact=True)).to_be_visible()
    await page.clock.fast_forward(60000)
    assert len(attempts) == 1
