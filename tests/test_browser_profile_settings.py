"""Profile session actions and settings presentation against a temporary gateway."""
import asyncio
import os
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from mcp_manager.app import create_app
from mcp_manager.config import Settings
from mcp_manager.database import McpServer


@pytest.fixture
async def running_gateway(tmp_path, monkeypatch):
    async def fake_fetch_latest_release():
        return {
            "version": "99.0.0",
            "pypi_url": "https://pypi.org/project/mcp-manager-gateway/",
            "releases_url": "https://github.com/eraycc/mcp-manager-gateway/releases",
        }

    monkeypatch.setattr("mcp_manager.updates.fetch_latest_release", fake_fetch_latest_release)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = create_app(Settings(data_dir=tmp_path, database_url="", secret_key="profile-ui-test", public_url=url))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(300):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(.02)
        assert server.started
        async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=15) as web:
            assert (await web.post("/api/v1/auth/register", json={"username": "admin", "password": "password12345"})).status_code == 200
            assert (await web.post("/api/v1/auth/login", json={"username": "admin", "password": "password12345"})).status_code == 200
            web.headers["X-CSRF-Token"] = web.cookies["mcp_csrf"]
            yield app, web, url, None, None
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()


async def login(page, url):
    await page.goto(url)
    await page.get_by_label("用户名", exact=True).fill("admin")
    await page.get_by_label("密码", exact=True).fill("password12345")
    await page.get_by_role("button", name="登录", exact=True).click()
    await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()


@pytest.mark.asyncio
async def test_profile_hides_disabled_services(running_gateway):
    app, _web, url, _token, _row = running_gateway
    async with app.state.db.locked() as session:
        session.add_all([
            McpServer(
                slug="profile-available",
                name="Available Profile MCP",
                transport="stdio",
                mode="lazy",
                config=app.state.catalog.seal({"command": "never"}),
            ),
            McpServer(
                slug="profile-disabled",
                name="Disabled Profile MCP",
                transport="stdio",
                mode="disabled",
                config=app.state.catalog.seal({"command": "never"}),
            ),
        ])

    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(
            headless=True,
            executable_path=executable if os.path.exists(executable) else None,
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await login(page, url)
        await page.goto(url + "/#/profile")
        services = page.locator("section.card").filter(
            has=page.get_by_role("heading", name="可访问的服务", exact=True)
        )
        await expect(services.get_by_text("Available Profile MCP", exact=True)).to_be_visible()
        await expect(services.get_by_text("Disabled Profile MCP", exact=True)).to_have_count(0)
        await browser.close()


@pytest.mark.asyncio
async def test_profile_noncurrent_session_revoke_and_permanent_delete(running_gateway):
    _app, _web, url, _token, _row = running_gateway
    async with httpx.AsyncClient(base_url=url, trust_env=False, headers={"User-Agent": "Session QA device"}) as other:
        assert (await other.post("/api/v1/auth/login", json={"username": "admin", "password": "password12345"})).status_code == 200
        async with async_playwright() as pw:
            executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
            browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await login(page, url)
            await page.goto(url + "/#/profile")
            current = page.locator(".session-row").filter(has=page.get_by_text("当前会话", exact=True))
            await expect(current).to_have_count(1)
            await expect(current.get_by_role("button")).to_have_count(0)
            device = page.locator(".session-row").filter(has_text="Session QA device")
            await expect(device).to_contain_text("登录 IP：127.0.0.1")
            await expect(device).to_contain_text("User-Agent：Session QA device")
            await device.get_by_role("button", name="撤销会话", exact=True).click()
            await page.get_by_role("button", name="确认执行", exact=True).click()
            await expect(device).to_contain_text("已撤销")
            await expect(device.get_by_role("button", name="撤销会话", exact=True)).to_have_count(0)
            assert (await other.get("/api/v1/me")).status_code == 401
            await device.get_by_role("button", name="永久删除", exact=True).click()
            await page.get_by_role("button", name="确认执行", exact=True).click()
            await expect(device).to_have_count(0)
            # Permanent deletion also invalidates a still-active login.
            assert (await other.post("/api/v1/auth/login", json={"username": "admin", "password": "password12345"})).status_code == 200
            await page.reload()
            await expect(device.get_by_role("button", name="撤销会话", exact=True)).to_be_visible()
            await device.get_by_role("button", name="永久删除", exact=True).click()
            await page.get_by_role("button", name="确认执行", exact=True).click()
            await expect(device).to_have_count(0)
            assert (await other.get("/api/v1/me")).status_code == 401
            await page.get_by_role("tab", name="本地 stdio", exact=True).click()
            command = page.locator("#connect_tab-panel-stdio")
            for alias in ("mmg", "mcp-manager", "mcp-manager-gateway"):
                await expect(command).to_contain_text(alias + " stdio --url " + url + "/mcp")
            assert errors == []
            await browser.close()


@pytest.mark.asyncio
async def test_settings_about_helpers_and_responsive_title(running_gateway):
    _app, web, url, _token, _row = running_gateway
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await login(page, url)
        for label in ("切换语言", "显示模式", "账户菜单"):
            await expect(page.locator(f'summary[aria-label="{label}"]')).to_be_visible()
        approval_path = await page.locator('a[aria-label="MCP 审批"] svg path').get_attribute("d")
        logs_path = await page.locator('a[aria-label="调用日志"] svg path').get_attribute("d")
        assert approval_path != logs_path
        await page.locator('summary[aria-label="显示模式"]').click()
        theme_menu = page.locator(".action-menu-panel").filter(has_text="显示模式")
        for label in ("浅色", "深色", "系统"):
            await expect(theme_menu.get_by_role("button", name=label, exact=True)).to_be_visible()
        await page.locator('summary[aria-label="显示模式"]').click()
        await page.locator('summary[aria-label="账户菜单"]').click()
        account_menu = page.locator(".account-menu .action-menu-panel")
        await expect(account_menu.get_by_text("admin", exact=True)).to_be_visible()
        await expect(account_menu.get_by_text("管理员", exact=True)).to_be_visible()
        await expect(account_menu.get_by_role("button", name="个人资料", exact=True)).to_be_visible()
        await expect(account_menu.get_by_role("button", name="系统设置", exact=True)).to_be_visible()
        await expect(account_menu.get_by_role("button", name="退出登录", exact=True)).to_be_visible()
        await page.locator('summary[aria-label="账户菜单"]').click()
        await expect(page.locator('a[aria-label="系统设置"] .notification-dot')).to_have_text("1")
        await page.goto(url + "/#/settings")
        title = page.get_by_label("站点名称", exact=True)
        await expect(title).to_be_visible()
        assert (await title.bounding_box())["width"] <= 440
        await page.get_by_role("tab", name="翻译设置", exact=True).click()
        await expect(page.get_by_role("heading", name="翻译设置", exact=True)).to_be_visible()
        await expect(page.get_by_label("启用全局网页翻译", exact=True)).to_be_visible()
        await page.get_by_role("tab", name="运行与日志", exact=True).click()
        await expect(page.get_by_text("设为 0 时不因空闲超时回收服务。", exact=True)).to_be_visible()
        await expect(page.get_by_text("设为 0 时无限保留；设为 7 时自动清理超过 7 天的日志。", exact=True)).to_be_visible()
        await page.get_by_role("tab", name="关于", exact=True).click()
        about = page.get_by_role("tabpanel", name="关于", exact=True)
        await expect(about).to_be_visible()
        metadata = (await web.get("/api/v1/about")).json()
        await expect(about.get_by_text(metadata["version"], exact=True)).to_be_visible()
        await expect(about.get_by_text("99.0.0", exact=True)).to_be_visible()
        await expect(about.get_by_label("自动检测更新", exact=True)).to_be_checked()
        await expect(page.get_by_role("button", name="保存设置", exact=True)).to_be_visible()
        for label, href in [
            ("项目主页", "https://github.com/eraycc/mcp-manager-gateway"),
            ("GitHub Releases", "https://github.com/eraycc/mcp-manager-gateway/releases"),
            ("PyPI 发布页", "https://pypi.org/project/mcp-manager-gateway/"),
            ("作者 eraycc", "https://github.com/eraycc"),
            ("问题反馈", "https://github.com/eraycc/mcp-manager-gateway/issues"),
        ]:
            link = about.get_by_role("link", name=label, exact=True)
            await expect(link).to_have_attribute("href", href)
            await expect(link).to_have_attribute("rel", "noopener noreferrer")
        await about.get_by_role("button", name="跳过本次更新", exact=True).click()
        await expect(page.locator('a[aria-label="系统设置"] .notification-dot')).to_have_count(0)
        await about.get_by_label("自动检测更新", exact=True).uncheck()
        async with page.expect_response(
            lambda response: response.url.endswith("/api/v1/settings")
            and response.request.method == "PATCH"
        ) as save_response:
            await page.get_by_role("button", name="保存设置", exact=True).click()
        assert (await save_response.value).status == 200
        await expect(about.get_by_label("自动检测更新", exact=True)).not_to_be_checked()
        assert (await web.get("/api/v1/settings")).json()["update_auto_check"] is False
        shots = Path(__file__).parents[1] / "artifacts/qa"
        shots.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(shots / "settings-about-desktop.png"), full_page=True)
        await page.reload()
        await expect(about).to_be_visible()
        for width in (390, 320):
            await page.set_viewport_size({"width": width, "height": 844})
            await expect(page.locator(".shell")).to_have_css("margin-left", "0px")
            overflow = await page.evaluate("""() => ({width: innerWidth, scroll: document.documentElement.scrollWidth, nodes: [...document.querySelectorAll('main *')].filter(n => n.getBoundingClientRect().right > innerWidth + 1).map(n => ({tag:n.tagName, cls:n.className, width:n.getBoundingClientRect().width})).slice(0, 12)})""")
            assert overflow["scroll"] <= width + 1, overflow
            await page.get_by_role("tab", name="基本设置", exact=True).click()
            await expect(title).to_be_visible()
            assert (await title.bounding_box())["width"] <= width - 28
            await page.get_by_role("tab", name="关于", exact=True).click()
        await page.screenshot(path=str(shots / "settings-about-mobile.png"), full_page=True)
        assert errors == []
        await browser.close()
