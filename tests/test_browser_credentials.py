"""Credential and isolation interactions in the browser UI."""
import os

import pytest
from playwright.async_api import async_playwright, expect
from test_browser_profile_settings import (
    login,
    running_gateway,  # noqa: F401
)

from mcp_manager.database import McpServer


async def seed_service(app, *, slug, name, isolation="user", auth=None, mode="lazy"):
    config = {"url": "https://mcp.invalid", "auth": auth or {"type": "none"}}
    async with app.state.db.locked() as session:
        row = McpServer(
            slug=slug,
            name=name,
            transport="streamable-http",
            mode=mode,
            isolation=isolation,
            config=app.state.catalog.seal(config),
        )
        session.add(row)
        await session.flush()
        row_id = row.id
    return row_id


@pytest.mark.asyncio
async def test_editor_limits_session_isolation_to_stdio(running_gateway):  # noqa: F811
    _app, _web, url, _token, _row = running_gateway
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(
            headless=True,
            executable_path=executable if os.path.exists(executable) else None,
        )
        page = await browser.new_page()
        await login(page, url)
        await page.goto(url + "/#/mcps")
        await page.get_by_role("button", name="新增服务", exact=True).click()

        isolation = page.get_by_label("实例隔离", exact=True)
        transport = page.get_by_label("传输协议", exact=True)
        assert await isolation.locator("option").all_text_contents() == [
            "服务共享",
            "按用户隔离",
            "按会话隔离",
        ]
        await isolation.select_option("session")
        await transport.select_option("streamable-http")
        await expect(isolation).to_have_value("user")
        assert await isolation.locator("option").all_text_contents() == [
            "服务共享",
            "按用户隔离",
        ]
        await expect(page.locator("#toast")).to_contain_text(
            "网络传输不支持会话隔离"
        )
        await page.get_by_label("认证方式", exact=True).select_option("oauth")
        config_isolation = page.get_by_label("OAuth 配置隔离", exact=True)
        assert await config_isolation.locator("option").all_text_contents() == [
            "共享配置",
            "独享配置",
        ]
        await expect(config_isolation).to_be_enabled()
        await isolation.select_option("service")
        await expect(page.get_by_label("OAuth 配置隔离", exact=True)).to_be_disabled()
        await isolation.select_option("user")
        await transport.select_option("stdio")
        assert await isolation.locator("option").all_text_contents() == [
            "服务共享",
            "按用户隔离",
            "按会话隔离",
        ]
        await expect(isolation).to_have_value("user")
        await browser.close()

@pytest.mark.asyncio
async def test_service_editor_secret_toggle_changes_input_type(running_gateway):  # noqa: F811
    app, _web, url, _token, _row = running_gateway
    service_id = await seed_service(
        app,
        slug="service-secret-toggle",
        name="Service Secret Toggle",
        isolation="service",
        auth={"type": "bearer", "token": "service-bearer-secret"},
        mode="disabled",
    )
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(
            headless=True,
            executable_path=executable if os.path.exists(executable) else None,
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await login(page, url)
        await page.goto(url + "/#/mcps")
        row = page.locator(f'tr[data-row-id="{service_id}"]')
        await row.get_by_role("button", name="编辑", exact=True).click()
        dialog = page.get_by_role(
            "dialog", name="编辑服务 · Service Secret Toggle", exact=True
        )
        token = dialog.get_by_label("Bearer Token", exact=True)
        await expect(token).to_have_value("[REDACTED]")
        await expect(token).to_have_attribute("type", "password")

        await dialog.get_by_role("button", name="显示敏感字段", exact=True).click()
        await expect(token).to_have_value("service-bearer-secret")
        await expect(token).to_have_attribute("type", "text")

        await dialog.get_by_role("button", name="隐藏敏感字段", exact=True).click()
        await expect(token).to_have_value("[REDACTED]")
        await expect(token).to_have_attribute("type", "password")
        await browser.close()


@pytest.mark.asyncio
async def test_profile_credential_action_matrix_and_disabled_filter(running_gateway):  # noqa: F811
    app, _web, url, _token, _row = running_gateway
    await seed_service(
        app,
        slug="profile-no-auth",
        name="Profile No Auth",
        auth={"type": "none"},
    )
    await seed_service(
        app,
        slug="profile-global-bearer",
        name="Profile Global Bearer",
        isolation="service",
        auth={"type": "bearer"},
    )
    await seed_service(
        app,
        slug="profile-user-bearer",
        name="Profile User Bearer",
        auth={"type": "bearer"},
    )
    shared_id = await seed_service(
        app,
        slug="profile-oauth-shared",
        name="Profile OAuth Shared",
        auth={"type": "oauth", "config_isolation": "shared"},
    )
    await seed_service(
        app,
        slug="profile-oauth-private",
        name="Profile OAuth Private",
        auth={"type": "oauth", "config_isolation": "user"},
    )
    await seed_service(
        app,
        slug="profile-disabled-auth",
        name="Profile Disabled Auth",
        auth={"type": "bearer"},
        mode="disabled",
    )
    async with app.state.db.locked() as session:
        await app.state.credentials.write(
            session,
            shared_id,
            "service",
            {
                "auth_config": {
                    "type": "oauth",
                    "authorization_url": "https://oauth.invalid/authorize",
                    "token_url": "https://oauth.invalid/token",
                    "client_id": "shared-client",
                }
            },
        )

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

        await expect(services.get_by_text("Profile Disabled Auth", exact=True)).to_have_count(0)
        await expect(
            services.locator(".credential-service").filter(has_text="Profile No Auth")
            .get_by_role("button", name="配置凭据", exact=True)
        ).to_have_count(0)
        await expect(
            services.locator(".credential-service").filter(has_text="Profile Global Bearer")
            .get_by_role("button", name="配置凭据", exact=True)
        ).to_have_count(0)
        bearer = services.locator(".credential-service").filter(has_text="Profile User Bearer")
        await expect(bearer.get_by_text("未配置", exact=True)).to_be_visible()
        await expect(bearer.get_by_role("button", name="配置凭据", exact=True)).to_be_visible()
        shared = services.locator(".credential-service").filter(has_text="Profile OAuth Shared")
        await expect(shared.get_by_text("待授权", exact=True)).to_be_visible()
        await expect(shared.get_by_role("button", name="授权", exact=True)).to_be_visible()
        private = services.locator(".credential-service").filter(has_text="Profile OAuth Private")
        await expect(private.get_by_text("未配置", exact=True)).to_be_visible()
        await expect(private.get_by_role("button", name="OAuth 配置", exact=True)).to_be_visible()
        await browser.close()
