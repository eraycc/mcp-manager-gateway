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
        await expect(single).to_contain_text("成功 2 工具")

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
