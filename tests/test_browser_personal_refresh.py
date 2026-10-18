"""Ordinary users can explicitly refresh only their saved, authorized scope."""
import os
from contextlib import asynccontextmanager

import pytest
from playwright.async_api import async_playwright, expect
from test_browser_credentials import seed_service
from test_browser_profile_settings import running_gateway  # noqa: F401

from mcp_manager.runtime import GatewayError


@pytest.mark.parametrize("kind", ["bearer", "oauth_shared"])
async def test_profile_refresh_is_gated_and_updates_only_personal_cache(running_gateway, kind):  # noqa: F811
    app, web, url, _, _ = running_gateway
    actor = (await web.get("/api/v1/me")).json()
    response = await web.post("/api/v1/users", json={
        "username": "reader", "password": "password12345", "role": "user", "scope_mode": "all",
    })
    assert response.status_code == 200, response.text
    reader = response.json()
    oauth = kind == "oauth_shared"
    service_id = await seed_service(
        app, slug="refresh-personal", name="Refresh personal MCP",
        auth={"type": "oauth", "config_isolation": "shared"} if oauth else {"type": "bearer"},
    )
    if oauth:
        async with app.state.db.locked() as session:
            await app.state.credentials.write(session, service_id, "service", {"auth_config": {
                "type": "oauth", "authorization_url": "https://auth.test/authorize",
                "token_url": "https://auth.test/token", "client_id": "shared-client", "client_secret": "shared-secret",
            }})
    calls, failing = [], {"value": False}

    class Connection:
        async def discover(self):
            if failing["value"]:
                raise GatewayError("discovery_failed", "Personal refresh failed")
            return {
                "tools": [{"name": "read", "inputSchema": {"type": "object"}}],
                "resources": [{"name": "Resource", "uri": "test://resource"}],
                "prompts": [{"name": "Prompt"}],
                "templates": [{"name": "Template", "uriTemplate": "test://{id}"}],
            }

    @asynccontextmanager
    async def connect(spec):
        calls.append(spec.credential_owner)
        yield Connection()
    app.state.runtime.connector = connect

    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page()
        errors, credential_saves = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: credential_saves.append(request.url)
                if request.method == "PUT" and request.url.endswith("/credentials") else None)
        await page.goto(url)
        await page.locator('input[autocomplete="username"]').fill("reader")
        await page.locator('input[autocomplete="current-password"]').fill("password12345")
        await page.locator(".auth form button[type=submit]").click()
        await expect(page.locator(".stats")).to_be_visible()
        await page.get_by_role("dialog", name="发现新版本", exact=True).get_by_role("button", name="关闭", exact=True).click()
        await page.goto(url + "/#/profile")
        service = page.locator(".credential-service").filter(has_text="Refresh personal MCP")
        await service.get_by_role("button", name="授权" if oauth else "配置凭据", exact=True).click()
        popup = page.get_by_role("dialog", name="授权服务" if oauth else "配置凭据", exact=True)
        refresh = popup.get_by_role("button", name="刷新能力缓存", exact=True)
        await expect(refresh).to_be_disabled()
        if oauth:
            async with app.state.db.locked() as session:
                await app.state.credentials.write(session, service_id, reader["id"], {
                    "oauth_token": {"access_token": "reader-access"},
                })
            await popup.get_by_role("button", name="重新检查授权状态", exact=True).click()
        else:
            await popup.get_by_label("Bearer Token", exact=True).fill("reader-token")
            await expect(refresh).to_be_disabled()  # Draft fields are not saved credentials.
            await popup.get_by_role("button", name="保存", exact=True).click()
            await expect(popup).to_have_count(0)
            await service.get_by_role("button", name="配置凭据", exact=True).click()
        await expect(refresh).to_be_enabled()
        saves = len(credential_saves)
        async with page.expect_response(
            lambda response: response.url.endswith(f"/api/v1/mcps/{service_id}/credentials/refresh")
        ) as pending:
            await refresh.click()
        assert (await pending.value).status == 200
        await expect(popup.locator(".personal-capability-refresh")).to_contain_text("工具 1")
        for text in ("工具 1", "资源 1", "提示词 1", "资源模板 1"):
            await expect(service.locator(".capability-summary")).to_contain_text(text)
        assert len(credential_saves) == saves
        assert calls and all(owner == reader["id"] for owner in calls)
        assert await app.state.credentials.load(service_id, actor["id"]) == {}
        failing["value"] = True
        await refresh.click()
        await expect(popup.locator(".personal-capability-refresh")).to_contain_text("Personal refresh failed")
        await expect(refresh).to_be_enabled()
        failing["value"] = False
        await refresh.click()
        await expect(popup.locator(".personal-capability-refresh")).to_contain_text("工具 1")
        assert errors == []
        await browser.close()
