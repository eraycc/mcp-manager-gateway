"""Capability descriptions stay readable inside cards at every console entry."""
import os

import pytest
from playwright.async_api import async_playwright, expect
from test_gateway import running_gateway  # noqa: F401


@pytest.mark.parametrize("entry", ["mcps", "proposals", "profile"])
async def test_capability_descriptions_wrap_inside_cards(running_gateway, entry):  # noqa: F811
    _app, _web, url, _token, row = running_gateway
    descriptions = [
        "identifier_" * 80,
        "https://example.test/" + "very-long-path-segment/" * 60,
        "中文工具描述，需要完整显示且不能越过卡片边界。" * 30,
        "Ordinary English tool description with spaces. " * 30,
    ]
    items = [
        {"name": f"tool_{index}", "description": description}
        for index, description in enumerate(descriptions)
    ]
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(
            headless=True, executable_path=executable if os.path.exists(executable) else None,
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.route("**/api/v1/about", lambda route: route.fulfill(json={
            "version": "1.3.1", "latest_version": "1.3.1", "update_available": False,
        }))
        await page.route("**/api/v1/mcps/*/capabilities?*", lambda route: route.fulfill(json={
            "items": items, "total": len(items), "next_cursor": None,
        }))
        await page.route("**/api/v1/mcp-proposals?*", lambda route: route.fulfill(json={
            "items": [{
                "id": "wrap-proposal", "name": "Wrap proposal", "transport": "stdio",
                "status": "pending", "test_status": "success", "test_result": {},
            }], "total": 1, "total_pages": 1,
        }))
        await page.route("**/api/v1/mcp-proposals/*/test-capabilities?*", lambda route: route.fulfill(json={
            "items": items, "total": len(items), "next_cursor": None,
        }))
        await page.goto(url)
        await page.get_by_label("用户名", exact=True).fill("admin")
        await page.get_by_label("密码", exact=True).fill("password12345")
        await page.get_by_role("button", name="登录", exact=True).click()
        await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()
        await page.goto(url + "/#/" + entry)
        if entry == "mcps":
            service = page.locator(f'tr[data-row-id="{row["id"]}"]')
            await service.get_by_role("button", name="工具 / 测试", exact=True).click()
        elif entry == "proposals":
            await page.get_by_role("button", name="能力目录", exact=True).click()
        else:
            await page.get_by_role("button", name="查看能力", exact=True).click()
        directory = page.locator("dialog").filter(has=page.locator(".capability-browser"))
        cards = directory.locator(".capability-card")
        await expect(cards).to_have_count(len(items))
        for width in (1280, 390, 320):
            await page.set_viewport_size({"width": width, "height": 900})
            for index, description in enumerate(descriptions):
                paragraph = cards.nth(index).locator("p")
                assert await paragraph.text_content() == description
                overflow = await paragraph.evaluate("""node => {
                    const card = node.closest('.capability-card').getBoundingClientRect();
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    return [...range.getClientRects()].filter(rect =>
                        rect.left < card.left || rect.right > card.right + 1 ||
                        rect.top < card.top || rect.bottom > card.bottom + 1
                    ).map(rect => ({left: rect.left, right: rect.right, cardRight: card.right}));
                }""")
                assert overflow == [], (entry, width, index, overflow[:3])
            assert await directory.evaluate(
                "node => node.scrollWidth <= node.clientWidth + 1"
            )
        assert errors == []
        await browser.close()
