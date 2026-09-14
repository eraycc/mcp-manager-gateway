"""User-facing embedding settings and reusable token controls."""
import os

from playwright.async_api import async_playwright, expect

from test_browser_profile_settings import login, running_gateway  # noqa: F401


async def test_embedding_settings_persist_masked_key_and_show_probe_result(running_gateway):
    app, web, url, _, _ = running_gateway
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await login(page, url)
        await page.goto(url + "/#/settings")
        await page.get_by_role("tab", name="工具检索", exact=True).click(timeout=5000)
        await expect(page.get_by_label("最低语义相似度", exact=True)).to_have_value("0.5")
        await expect(page.get_by_text("默认 0.5", exact=False)).to_be_visible()
        await page.get_by_label("启用向量语义检索", exact=True).check()
        await page.get_by_label("Embedding 接口地址", exact=True).fill("http://127.0.0.1:9/v1")
        await page.get_by_label("Embedding 模型", exact=True).fill("test-model")
        await page.get_by_label("Embedding API Key", exact=True).fill("test-key-not-visible")
        await page.get_by_label("最低语义相似度", exact=True).fill("0.7")
        await page.get_by_role("button", name="保存检索设置", exact=True).click()
        await expect(page.get_by_label("Embedding API Key", exact=True)).to_have_value("[REDACTED]")
        saved = (await web.get("/api/v1/settings/embedding")).json()
        assert saved["enabled"] and saved["model"] == "test-model" and saved["api_key"] == "[REDACTED]"
        await page.get_by_role("button", name="测试已保存的连接", exact=True).click()
        await expect(page.get_by_role("status").filter(has_text="连接不可用")).to_be_visible()
        await page.reload()
        await expect(page.get_by_label("Embedding 模型", exact=True)).to_have_value("test-model")
        await page.set_viewport_size({"width": 390, "height": 844})
        overflow = await page.evaluate("""() => ({width: innerWidth, scroll: document.documentElement.scrollWidth,
            nodes: [...document.querySelectorAll('main *')].filter(n => n.getBoundingClientRect().right > innerWidth + 1)
            .map(n => ({tag:n.tagName, cls:n.className, width:n.getBoundingClientRect().width})).slice(0, 12)})""")
        assert overflow["scroll"] <= overflow["width"] + 1, overflow
        assert errors == []
        await browser.close()


async def test_admin_token_views_copy_and_editor_reveal(running_gateway):
    app, web, url, _, _ = running_gateway
    other = (await web.post("/api/v1/users", json={"username": "otheruser", "password": "password12345"})).json()
    own = (await web.post("/api/v1/tokens", json={"name": "my-test-token"})).json()
    await web.post("/api/v1/tokens", json={"name": "other-test-token", "user_id": other["id"]})
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        context = await browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = await context.new_page()
        await login(page, url)
        await page.goto(url + "/#/tokens")
        await expect(page.get_by_role("row").filter(has_text="my-test-token")).to_be_visible()
        await expect(page.get_by_role("row").filter(has_text="other-test-token")).to_have_count(0)
        row = page.get_by_role("row").filter(has_text="my-test-token")
        await row.get_by_role("button", name="复制", exact=True).click()
        await expect(page.get_by_role("status").filter(has_text="令牌已复制")).to_be_visible()
        assert await page.evaluate("navigator.clipboard.readText()") == own["token"]
        await row.get_by_role("button", name="编辑", exact=True).click()
        await expect(page.get_by_label("访问令牌值", exact=True)).to_have_value(own["token"])
        await page.keyboard.press("Escape")
        await page.get_by_label("令牌视图", exact=True).select_option("all")
        await expect(page.get_by_role("row").filter(has_text="other-test-token")).to_be_visible()
        await page.get_by_label("按用户名筛选", exact=True).fill("otheruser")
        await page.get_by_label("按用户名筛选", exact=True).press("Enter")
        await expect(page.get_by_role("row").filter(has_text="my-test-token")).to_have_count(0)
        await browser.close()


async def test_legacy_and_unreadable_token_secrets_keep_editor_usable(running_gateway):
    from mcp_manager.database import ApiToken

    app, web, url, _, _ = running_gateway
    created = []
    for label, value in [("legacy-value", None), ("damaged-value", "not-fernet")]:
        row = (await web.post("/api/v1/tokens", json={"name": label})).json()
        async with app.state.db.locked() as session:
            (await session.get(ApiToken, row["id"])).token_secret = value
        created.append(row)
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(headless=True, executable_path=executable if os.path.exists(executable) else None)
        page = await browser.new_page()
        await login(page, url)
        await page.goto(url + "/#/tokens")
        legacy = page.get_by_role("row").filter(has_text="legacy-value")
        await expect(legacy.get_by_role("button", name="复制", exact=True)).to_have_count(0)
        for item in created:
            row = page.get_by_role("row").filter(has_text=item["name"])
            await row.get_by_role("button", name="编辑", exact=True).click()
            dialog = page.get_by_role("dialog", name="编辑访问令牌", exact=True)
            await expect(dialog).to_be_visible()
            await expect(dialog).to_contain_text("轮换一次")
            await page.keyboard.press("Escape")
            rotated = (await web.post("/api/v1/tokens/" + item["id"] + "/rotate")).json()
            await page.reload()
            await row.get_by_role("button", name="编辑", exact=True).click()
            await expect(page.get_by_label("访问令牌值", exact=True)).to_have_value(rotated["token"])
            await page.keyboard.press("Escape")
        await browser.close()
