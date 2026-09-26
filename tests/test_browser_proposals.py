import asyncio

import pytest
from playwright.async_api import async_playwright, expect
from sqlalchemy import select
from test_gateway import running_gateway  # noqa: F401

from mcp_manager.database import ApiToken, User
from mcp_manager.proposals import submit_proposals


@pytest.mark.asyncio
async def test_proposal_test_feedback_and_batch_job_dialog(running_gateway, monkeypatch):  # noqa: F811
    app, web, url, _token, _row = running_gateway
    created = await web.post("/api/v1/tokens", json={
        "name": "proposal-browser", "scope_mode": "all",
        "enable_mcp_proposal": True,
    })
    async with app.state.db.session() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        token = await session.get(ApiToken, created.json()["id"])

    async def submit(name, command):
        result = await submit_proposals(app, user, token, {
            "name": name,
            "transport": "stdio",
            "config": {"command": command},
            "purpose": "browser proposal test",
        })
        return result["items"][0]["id"]

    single_id = await submit("Single proposal", "single")
    batch_id = await submit("Batch proposal", "batch")
    entered = {"single": asyncio.Event(), "batch": asyncio.Event()}
    release = {"single": asyncio.Event(), "batch": asyncio.Event()}

    async def discover(spec, lease_id):
        command = spec.config["command"]
        entered[command].set()
        await release[command].wait()
        return {
            "tools": [{"name": "one"}, {"name": "two"}],
            "resources": [], "prompts": [], "templates": [],
            "capability_errors": [
                {"capability": "resources", "error": "Method not found"}
            ],
        }

    monkeypatch.setattr(app.state.runtime, "discover", discover)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.get_by_label("用户名", exact=True).fill("admin")
        await page.get_by_label("密码", exact=True).fill("password12345")
        await page.get_by_role("button", name="登录", exact=True).click()
        await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()
        await page.goto(url + "/#/proposals")

        await expect(page.get_by_role("columnheader", name="测试结果", exact=True)).to_be_visible()
        single = page.locator(f'tr[data-row-id="{single_id}"]')
        await single.get_by_role("button", name="工具 / 测试", exact=True).click()
        await expect(page.locator("#toast")).to_have_text("正在测试 MCP 连接…")
        await asyncio.wait_for(entered["single"].wait(), 5)
        release["single"].set()
        await expect(page.get_by_role("dialog", name="测试结果", exact=True)).to_be_visible()
        await expect(page.locator("#toast")).to_have_text("测试成功")
        await page.get_by_role("dialog", name="测试结果", exact=True).get_by_role(
            "button", name="关闭", exact=True
        ).click()
        await expect(single).to_contain_text("部分可用")
        await expect(single).to_contain_text("资源不可用（Method not found）")

        batch = page.locator(f'tr[data-row-id="{batch_id}"]')
        await batch.get_by_role("checkbox").check()
        await page.get_by_role("button", name="批量测试", exact=True).click()
        await expect(page.locator("#toast")).to_have_text("已提交批量测试")
        dialog = page.get_by_role("dialog", name="MCP 批量测试", exact=True)
        await expect(dialog).to_be_visible()
        await asyncio.wait_for(entered["batch"].wait(), 5)
        await expect(dialog.get_by_role("button", name="停止测试", exact=True)).to_be_visible()
        await dialog.get_by_role("button", name="停止测试", exact=True).click()
        await expect(dialog.get_by_role("button", name="关闭", exact=True).last).to_be_visible(
            timeout=5000
        )
        await dialog.get_by_role("button", name="关闭", exact=True).last.click()
        await expect(batch).to_contain_text("已取消")

        await page.reload()
        await expect(page.locator(f'tr[data-row-id="{batch_id}"]')).to_contain_text("已取消")
        assert errors == []
        await browser.close()



@pytest.mark.asyncio
async def test_proposal_uses_shared_full_editor_and_global_secret_toggle(running_gateway):  # noqa: F811
    app, web, url, _token, _row = running_gateway
    created = await web.post("/api/v1/tokens", json={
        "name": "proposal-editor-browser", "scope_mode": "all",
        "enable_mcp_proposal": True,
    })
    async with app.state.db.session() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        token = await session.get(ApiToken, created.json()["id"])
    result = await submit_proposals(app, user, token, {
        "name": "OAuth editor proposal",
        "transport": "streamable-http",
        "description": "agent draft",
        "tags": ["agent"],
        "config": {
            "url": "https://example.test/mcp",
            "headers": {"X-Private": "header-secret"},
            "auth": {
                "type": "oauth",
                "authorization_url": "https://auth.example.test/authorize",
                "token_url": "https://auth.example.test/token",
                "client_id": "browser-client",
                "client_secret": "browser-secret",
                "scopes": ["tools.read"],
            },
        },
    })
    proposal_id = result["items"][0]["id"]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.get_by_label("用户名", exact=True).fill("admin")
        await page.get_by_label("密码", exact=True).fill("password12345")
        await page.get_by_role("button", name="登录", exact=True).click()
        await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()
        await page.goto(url + "/#/proposals")
        await expect(page.get_by_role("heading", name="MCP 审批", exact=True)).to_be_visible()
        await page.wait_for_timeout(200)
        assert errors == []

        row = page.locator(f'tr[data-row-id="{proposal_id}"]')
        await expect(row).to_be_visible()
        await row.get_by_role("button", name="配置", exact=True).click()
        dialog = page.get_by_role(
            "dialog", name="配置 MCP 提议 · OAuth editor proposal", exact=True
        )
        await expect(dialog).to_be_visible()
        await expect(dialog.get_by_label("服务名称", exact=True)).to_have_value(
            "OAuth editor proposal"
        )
        await expect(dialog.get_by_label("服务标识", exact=False)).to_have_value("")
        await expect(dialog.get_by_label("描述（建议填写）", exact=False)).to_have_value(
            "agent draft"
        )
        await expect(dialog.get_by_label("传输协议", exact=True)).to_have_value(
            "streamable-http"
        )
        await expect(dialog.get_by_label("启动策略", exact=True)).to_have_value("lazy")
        await expect(dialog.get_by_label("实例隔离", exact=True)).to_have_value("service")
        await expect(dialog.get_by_label("服务 URL", exact=True)).to_have_value(
            "https://example.test/mcp"
        )
        await expect(dialog.get_by_label("认证方式", exact=True)).to_have_value("oauth")
        await expect(dialog.get_by_label("OAuth 配置隔离", exact=True)).to_have_value("")
        secret = dialog.get_by_label("Client Secret", exact=True)
        await expect(secret).to_have_value("[REDACTED]")

        await dialog.get_by_role("button", name="显示敏感字段", exact=True).click()
        await expect(secret).to_have_value("browser-secret")
        await dialog.get_by_role("button", name="JSON 配置", exact=True).click()
        editor = dialog.get_by_label("完整配置 JSON", exact=False)
        assert "browser-secret" in await editor.input_value()
        await dialog.get_by_role("button", name="隐藏敏感字段", exact=True).click()
        assert "[REDACTED]" in await editor.input_value()
        assert "browser-secret" not in await editor.input_value()

        await dialog.get_by_role("button", name="表单配置", exact=True).click()
        await dialog.get_by_label("描述（建议填写）", exact=False).fill("approved draft")
        await dialog.get_by_role("button", name="保存审批配置", exact=True).click()
        await expect(dialog).to_be_hidden()

        saved = (await web.get(f"/api/v1/mcp-proposals/{proposal_id}?reveal=true")).json()
        assert saved["payload"]["description"] == "approved draft"
        assert saved["payload"]["config"]["auth"]["client_secret"] == "browser-secret"

        row = page.locator(f'tr[data-row-id="{proposal_id}"]')
        await row.get_by_role("button", name="工具 / 测试", exact=True).click()
        await expect(page.locator("#toast")).to_have_text(
            "OAuth 类型需要在审批通过并授权后验证"
        )
        assert errors == []
        await browser.close()
