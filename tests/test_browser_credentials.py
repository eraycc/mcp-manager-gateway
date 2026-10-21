"""Credential and isolation interactions in the browser UI."""
import json
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
    private_id = await seed_service(
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
        await expect(shared.get_by_role("button", name="OAuth 配置", exact=True)).to_have_count(0)
        actor = (await _web.get("/api/v1/me")).json()
        async with app.state.db.locked() as session:
            await app.state.credentials.write(session, private_id, actor["id"], {
                "auth_config": {
                    "type": "oauth", "authorization_url": "https://oauth.invalid/authorize",
                    "token_url": "https://oauth.invalid/token", "client_id": "personal-client",
                    "client_secret": "personal-secret",
                },
            })
            await app.state.credentials.write(session, shared_id, actor["id"], {
                "oauth_token": {"access_token": "shared-user-access"},
            })
        await page.reload()
        await expect(shared.get_by_role("button", name="授权", exact=True)).to_be_visible()
        await expect(shared.get_by_role("button", name="OAuth 配置", exact=True)).to_have_count(0)
        await expect(private.get_by_text("待授权", exact=True)).to_be_visible()
        await expect(private.get_by_role("button", name="OAuth 配置", exact=True)).to_be_visible()
        await expect(private.get_by_role("button", name="授权", exact=True)).to_be_visible()
        await browser.close()



@pytest.mark.parametrize("auth_type", ["bearer", "basic", "api_key", "oauth"])
@pytest.mark.parametrize("username", ["admin", "reader"])
async def test_profile_keeps_saved_personal_credentials_editable(running_gateway, auth_type, username):  # noqa: F811
    from contextlib import asynccontextmanager

    from test_personal_credentials_api import Connection

    app, web, url, _, _ = running_gateway
    admin = (await web.get("/api/v1/me")).json()
    response = await web.post("/api/v1/users", json={
        "username": "reader", "password": "password12345", "role": "user", "scope_mode": "all",
    })
    assert response.status_code == 200, response.text
    reader = response.json()
    actors = {"admin": admin, "reader": reader}
    auth = {"type": auth_type}
    if auth_type == "oauth":
        auth["config_isolation"] = "user"
    service_id = await seed_service(
        app, slug="personal-edit", name="Personal editable MCP", auth=auth,
    )
    original = {}
    async with app.state.db.locked() as session:
        for name, actor in actors.items():
            config = ({"type": "bearer", "token": name + "-old-token"} if auth_type == "bearer" else
                      {"type": "basic", "username": name, "password": name + "-old-password"} if auth_type == "basic" else
                      {"type": "api_key", "header": "X-Personal-Key", "value": name + "-old-key"} if auth_type == "api_key" else {
                "type": "oauth", "authorization_url": "https://oauth.invalid/authorize",
                "token_url": "https://oauth.invalid/token", "client_id": name + "-old-client",
                "client_secret": name + "-old-secret", "scopes": ["read"],
            })
            original[name] = {"auth_config": config}
            if auth_type == "oauth":
                original[name]["oauth_token"] = {"access_token": name + "-access-token"}
            await app.state.credentials.write(session, service_id, actor["id"], original[name])

    @asynccontextmanager
    async def connect(_spec):
        yield Connection()
    app.state.runtime.connector = connect

    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(
            headless=True, executable_path=executable if os.path.exists(executable) else None,
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.locator('input[autocomplete="username"]').fill(username)
        await page.locator('input[autocomplete="current-password"]').fill("password12345")
        await page.locator(".auth form button[type=submit]").click()
        await expect(page.locator(".stats")).to_be_visible()
        await page.get_by_role("dialog", name="发现新版本", exact=True).get_by_role("button", name="关闭", exact=True).click()
        await page.goto(url + "/#/profile")
        if username == "reader":
            await expect(page.locator('a[aria-label="MCP 服务"]')).to_have_count(0)
        service = page.locator(".credential-service").filter(has_text="Personal editable MCP")
        label = "OAuth 配置" if auth_type == "oauth" else "配置凭据"
        edit = service.get_by_role("button", name=label, exact=True)
        await expect(edit).to_be_visible()
        await edit.click()
        popup = page.get_by_role("dialog", name=label, exact=True)
        if auth_type == "bearer":
            await popup.get_by_label("Bearer Token", exact=True).fill(username + "-new-token")
        elif auth_type == "basic":
            await popup.get_by_label("用户名", exact=True).fill(username + "-new-name")
            await popup.get_by_label("密码", exact=True).fill(username + "-new-password")
        elif auth_type == "api_key":
            await popup.get_by_label("请求头名称（例如 X-API-Key）", exact=True).fill("X-Personal-Key")
            await popup.get_by_label("API Key", exact=True).fill(username + "-new-key")
        else:
            await popup.get_by_label("OAuth 客户端注册方式", exact=True).select_option("pre_registration")
            for field, value in [
                ("授权 URL", "https://oauth.invalid/new-authorize"),
                ("Token URL", "https://oauth.invalid/new-token"),
                ("Client ID", username + "-new-client"),
                ("Client Secret", username + "-new-secret"),
                ("Scopes（空格分隔）", "read write"),
            ]:
                await popup.get_by_label(field, exact=True).fill(value)
        async with page.expect_response(
            lambda response: response.url.endswith(f"/api/v1/mcps/{service_id}/credentials")
            and response.request.method == "PUT"
        ) as pending:
            await popup.get_by_role("button", name="保存", exact=True).click()
        saved = await pending.value
        assert saved.status == 200, await saved.text()
        await expect(popup).to_have_count(0)
        await expect(edit).to_be_visible()
        await edit.click()
        await expect(page.get_by_role("dialog", name=label, exact=True)).to_be_visible()
        value = await app.state.credentials.load(service_id, actors[username]["id"])
        if auth_type == "bearer":
            assert value["auth_config"]["token"] == username + "-new-token"
        elif auth_type == "basic":
            assert value["auth_config"]["username"] == username + "-new-name"
            assert value["auth_config"]["password"] == username + "-new-password"
        elif auth_type == "api_key":
            assert value["auth_config"]["header"] == "X-Personal-Key"
            assert value["auth_config"]["value"] == username + "-new-key"
        else:
            assert value["auth_config"]["client_id"] == username + "-new-client"
            assert value["auth_config"]["client_secret"] == username + "-new-secret"
            assert value["auth_config"]["scopes"] == ["read", "write"]
        other = "reader" if username == "admin" else "admin"
        assert await app.state.credentials.load(service_id, actors[other]["id"]) == original[other]
        assert errors == []
        await browser.close()


@pytest.mark.asyncio
async def test_editor_restores_auth_drafts_without_serializing_inactive_secrets(running_gateway):  # noqa: F811
    _app, _web, url, _token, _row = running_gateway
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(
            headless=True, executable_path=executable if os.path.exists(executable) else None,
        )
        try:
            page = await browser.new_page()
            await login(page, url)
            await page.goto(url + "/#/mcps")
            await page.get_by_role("button", name="新增服务", exact=True).click()
            await page.get_by_label("传输协议", exact=True).select_option("streamable-http")
            auth = page.get_by_label("认证方式", exact=True)
            await auth.select_option("bearer")
            await page.get_by_label("Bearer Token", exact=True).fill("draft-bearer")
            await auth.select_option("basic")
            await expect(page.get_by_label("用户名", exact=True)).to_have_value("")
            await expect(page.get_by_label("密码", exact=True)).to_have_value("")
            await page.get_by_label("用户名", exact=True).fill("draft-user")
            await page.get_by_label("密码", exact=True).fill("draft-password")
            await auth.select_option("bearer")
            await expect(page.get_by_label("Bearer Token", exact=True)).to_have_value("draft-bearer")

            await auth.select_option("oauth")
            registration = page.get_by_label("OAuth 客户端注册方式", exact=True)
            await expect(registration).to_have_value("prefer_cimd_fallback_dcr")
            await registration.select_option("pre_registration")
            await page.get_by_label("Client ID", exact=True).fill("manual-client")
            await page.get_by_label("Client Secret", exact=True).fill("manual-secret")
            await registration.select_option("prefer_cimd_fallback_dcr")
            await auth.select_option("basic")
            await expect(page.get_by_label("用户名", exact=True)).to_have_value("draft-user")
            await expect(page.get_by_label("密码", exact=True)).to_have_value("draft-password")
            await auth.select_option("oauth")
            await expect(registration).to_have_value("prefer_cimd_fallback_dcr")
            await expect(page.get_by_label("客户端名称（client_name）", exact=True)).to_have_value("Codex")
            await registration.select_option("pre_registration")
            await expect(page.get_by_label("Client ID", exact=True)).to_have_value("")
            await expect(page.get_by_label("Client Secret", exact=True)).to_have_value("")

            await auth.select_option("bearer")
            await page.get_by_role("button", name="JSON 配置", exact=True).click()
            config = json.loads(await page.get_by_label("完整配置 JSON", exact=False).input_value())
            assert config["config"]["auth"] == {"type": "bearer", "token": "draft-bearer"}
        finally:
            await browser.close()
