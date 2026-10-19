"""Console export, cross-page batch selection, and cached capability summaries."""
import json
import os
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.async_api import async_playwright, expect
from test_browser_profile_settings import login, running_gateway  # noqa: F401


@pytest.fixture
async def console_page(running_gateway):  # noqa: F811
    _, _, url, _, _ = running_gateway
    async with async_playwright() as pw:
        executable = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        browser = await pw.chromium.launch(
            headless=True, executable_path=executable if os.path.exists(executable) else None,
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await login(page, url)
        yield page, url
        assert errors == []
        await browser.close()


def service(index):
    return {
        "id": f"row-{index}", "name": f"Service {index}", "username": f"user-{index}",
        "slug": f"service-{index}", "description": "Cached service description",
        "transport": "stdio", "mode": "lazy", "status": "pending", "role": "user",
        "created_at": "2026-01-01T00:00:00Z", "test_status": "pending",
        "scope_mode": "all", "cache_status": "ready", "cache_at": "2026-01-01T00:00:00Z",
        "tool_count": 2, "resource_count": 3, "prompt_count": 4, "template_count": 5,
    }


async def mock_records(page, path, rows, submissions):
    async def handler(route):
        request = route.request
        parsed = urlparse(request.url)
        if request.method != "GET":
            data = request.post_data_json
            submissions.append((parsed.path, data))
            count = len((data or {}).get("ids", []))
            if parsed.path.endswith(("/batch", "/delete")) and path in ("/mcps", "/logs"):
                result = {"id": "selection-job", "status": "completed", "total": count,
                          "completed": count, "succeeded": count}
            elif path == "/tokens":
                result = {}
            elif path == "/users":
                result = {"ok": True, "count": count}
            else:
                result = {"results": [{"id": item, "ok": True} for item in data.get("ids", [])]}
            await route.fulfill(json=result)
            return
        query = parse_qs(parsed.query)
        number = int(query.get("page", ["1"])[0])
        size = int(query.get("page_size", ["20"])[0])
        filtered = [row for row in rows if query.get("q", [""])[0] in row["name"]]
        await route.fulfill(json={
            "items": filtered[(number - 1) * size:number * size], "total": len(filtered),
            "page": number, "page_size": size, "total_pages": (len(filtered) + size - 1) // size,
        })
    await page.route("**/api/v1" + path + "?*", handler)
    await page.route("**/api/v1" + path + "/*", handler)


@pytest.mark.parametrize("module,path,action", [
    ("mcps", "/mcps", "设为禁用"),
    ("proposals", "/mcp-proposals", "批量删除"),
    ("tokens", "/tokens", "批量禁用"),
    ("logs", "/logs", "删除选中"),
    ("users", "/users", "批量禁用"),
])
async def test_cross_page_selection_and_confirmed_batch(console_page, module, path, action):
    page, url = console_page
    submissions = []
    await mock_records(page, path, [service(i) for i in range(23)], submissions)
    await page.goto(url + "/#/" + module + "?page_size=10")
    first = page.locator('tr[data-row-id="row-0"] input[type=checkbox]')
    await first.check()
    await page.get_by_role("button", name="下一页", exact=True).click()
    await expect(page.locator("tbody tr")).to_have_count(10)
    await expect(page.get_by_text("已选 1 项", exact=True)).to_be_visible()
    await page.locator('tr[data-row-id="row-10"] input[type=checkbox]').check()
    await page.get_by_role("button", name="上一页", exact=True).click()
    await expect(first).to_be_checked()
    await expect(page.get_by_text("已选 2 项", exact=True)).to_be_visible()

    await page.get_by_role("button", name=action, exact=True).click()
    confirmation = page.get_by_role("dialog", name="确认操作", exact=True)
    await expect(confirmation).to_be_visible()
    await expect(page.get_by_text("已选 2 项", exact=True)).to_be_visible()
    await confirmation.get_by_role("button", name="关闭", exact=True).click()
    assert submissions == []
    await expect(first).to_be_checked()
    await page.get_by_role("button", name=action, exact=True).click()
    await confirmation.get_by_role("button", name="确认执行", exact=True).click()
    await expect(page.get_by_text("已选 0 项", exact=True)).to_be_visible()
    if module == "tokens":
        assert {entry[0].rsplit("/", 1)[-1] for entry in submissions} == {"row-0", "row-10"}
    else:
        assert submissions[0][1]["ids"] == ["row-0", "row-10"]


async def test_current_page_select_all_jump_size_filter_and_list_scope(console_page):
    page, url = console_page
    await mock_records(page, "/mcps", [service(i) for i in range(23)], [])
    await page.goto(url + "/#/mcps?page_size=10")
    await page.get_by_role("checkbox", name="选择当前页", exact=True).check()
    await expect(page.get_by_text("已选 10 项", exact=True)).to_be_visible()
    await page.get_by_label("跳转页码", exact=True).fill("3")
    await page.get_by_role("button", name="跳转", exact=True).click()
    await expect(page.locator("tbody tr")).to_have_count(3)
    await expect(page.get_by_text("已选 10 项", exact=True)).to_be_visible()
    await page.get_by_role("checkbox", name="选择当前页", exact=True).check()
    await expect(page.get_by_text("已选 13 项", exact=True)).to_be_visible()
    await page.get_by_label("每页条数", exact=True).select_option("20")
    await expect(page.locator('tr[data-row-id="row-0"] input')).to_be_checked()
    await expect(page.get_by_role("checkbox", name="选择当前页", exact=True)).not_to_be_checked()
    assert await page.get_by_role("checkbox", name="选择当前页", exact=True).evaluate("(n) => n.indeterminate")
    await page.get_by_role("button", name="选择全部筛选结果", exact=True).click()
    await expect(page.get_by_text("已选 23 项", exact=True)).to_be_visible()
    await page.get_by_role("button", name="清空选择", exact=True).click()
    await expect(page.get_by_text("已选 0 项", exact=True)).to_be_visible()
    await page.locator('tr[data-row-id="row-0"] input').check()
    await page.get_by_role("searchbox", name="搜索", exact=True).fill("Service 1")
    await page.get_by_role("button", name="搜索", exact=True).click()
    await expect(page.get_by_text("已选 0 项", exact=True)).to_be_visible()
    await page.locator("tbody input[type=checkbox]").first.check()
    await page.locator('a[aria-label="仪表盘"]').click()
    await expect(page.get_by_role("heading", name="仪表盘", exact=True)).to_be_visible()
    await page.locator('a[aria-label="MCP 服务"]').click()
    await expect(page.get_by_text("已选 0 项", exact=True)).to_be_visible()


async def test_export_all_requires_confirmation_and_ignores_selection_and_filter(console_page, tmp_path):
    page, url = console_page
    submissions = []
    await mock_records(page, "/mcps", [service(i) for i in range(23)], [])
    exported = {"mcpServers": {"first": {"command": "first"}, "last": {"url": "https://example.com/mcp"}}}

    async def export(route):
        submissions.append(route.request.post_data_json)
        await route.fulfill(json=exported)
    await page.route("**/api/v1/mcps/export", export)
    await page.goto(url + "/#/mcps?page_size=10&q=Service+1")
    await expect(page.locator(".page-heading .actions .button-label")).to_have_text(["导入配置", "导出配置", "新增服务"])
    await page.locator("tbody input[type=checkbox]").first.check()
    await page.get_by_role("button", name="导出配置", exact=True).click()
    confirmation = page.get_by_role("dialog", name="确认操作", exact=True)
    await expect(confirmation).to_contain_text("全部 MCP 服务配置")
    await confirmation.get_by_role("button", name="关闭", exact=True).click()
    assert submissions == []
    await page.get_by_role("button", name="导出配置", exact=True).click()
    async with page.expect_download() as pending:
        await confirmation.get_by_role("button", name="确认执行", exact=True).click()
    download = await pending.value
    assert download.suggested_filename == "mcp-services.json"
    target = tmp_path / download.suggested_filename
    await download.save_as(target)
    assert json.loads(target.read_text(encoding="utf-8")) == exported
    assert submissions == [{"include_secrets": False}]
    await expect(page.get_by_text("已选 1 项", exact=True)).to_be_visible()


async def test_service_and_profile_show_cached_capability_counts_and_description(console_page):
    page, url = console_page
    rows = [{**service(0), "description": "Cached service description https://example.com/" + "x" * 200 + "中文" * 60}, {**service(1), "cache_status": "empty", "cache_at": None,
                         "description": "", "tool_count": 0},
            {**service(2), "cache_status": "ready", "tool_count": 0, "resource_count": 0,
             "prompt_count": 0, "template_count": 0}]
    await mock_records(page, "/mcps", rows, [])
    await page.route("**/api/v1/mcps/credentials/status-batch", lambda route: route.fulfill(json={"items": []}))
    await page.goto(url + "/#/mcps")
    await expect(page.get_by_role("columnheader", name="能力列表", exact=True)).to_be_visible()
    row = page.locator('tr[data-row-id="row-0"]')
    await expect(row.get_by_role("button", name="能力/测试", exact=True)).to_be_visible()
    for text in ("工具 2", "资源 3", "提示词 4", "资源模板 5"):
        await expect(row.locator(".capability-summary")).to_contain_text(text)
    await expect(page.locator('tr[data-row-id="row-1"]')).to_contain_text("暂无能力缓存")
    await page.goto(url + "/#/profile")
    available = page.locator(".credential-service").filter(has_text="Service 0")
    await expect(available).to_contain_text("Cached service description")
    for text in ("工具 2", "资源 3", "提示词 4", "资源模板 5"):
        await expect(available.locator(".capability-summary")).to_contain_text(text)
    await expect(page.locator(".credential-service").filter(has_text="Service 1").locator(".capability-summary")).to_have_count(0)
    await expect(page.locator(".credential-service").filter(has_text="Service 2").locator(".capability-summary")).to_contain_text("工具 0")
    await page.set_viewport_size({"width": 320, "height": 844})
    await expect(page.locator(".shell")).to_have_css("margin-left", "0px")
    overflow = await available.evaluate("""node => {
        const card = node.getBoundingClientRect(), range = document.createRange();
        range.selectNodeContents(node.querySelector('.credential-service-description'));
        return {width: innerWidth, right: card.right, scroll: node.scrollWidth, client: node.clientWidth,
                outside: [...range.getClientRects()].some(r => r.right > card.right + 1 || r.left < card.left)};
    }""")
    assert overflow["right"] <= overflow["width"] + 1, overflow
    assert overflow["scroll"] <= overflow["client"] + 1, overflow
    assert not overflow["outside"], overflow


@pytest.mark.parametrize("module,path,action", [
    ("users", "/users", "批量禁用"),
    ("proposals", "/mcp-proposals", "批量删除"),
    ("mcps", "/mcps", "设为禁用"),
])
async def test_all_matching_batch_respects_five_hundred_item_limit(console_page, module, path, action):
    page, url = console_page
    submissions = []
    await mock_records(page, path, [service(i) for i in range(501)], submissions)
    await page.goto(url + "/#/" + module)
    await page.get_by_role("button", name="选择全部筛选结果", exact=True).click()
    await expect(page.get_by_text("已选 501 项", exact=True)).to_be_visible()
    await page.get_by_role("button", name=action, exact=True).click()
    await page.get_by_role("button", name="确认执行", exact=True).click()
    await expect(page.get_by_text("已选 0 项", exact=True)).to_be_visible()
    assert [len(body["ids"]) for _, body in submissions] == [500, 1]
    assert [item for _, body in submissions for item in body["ids"]] == [f"row-{i}" for i in range(501)]


@pytest.mark.parametrize("outcome", ["http_error", "partial_failure"])
async def test_failed_batch_preserves_cross_page_selection(console_page, outcome):
    page, url = console_page
    await mock_records(page, "/mcp-proposals", [service(i) for i in range(23)], [])
    await page.goto(url + "/#/proposals?page_size=10")
    await page.locator('tr[data-row-id="row-0"] input').check()
    await page.get_by_role("button", name="下一页", exact=True).click()
    await page.locator('tr[data-row-id="row-10"] input').check()

    async def failure(route):
        if outcome == "http_error":
            await route.fulfill(status=503, json={"detail": "Temporary batch failure"})
        else:
            await route.fulfill(json={"results": [
                {"id": "row-0", "ok": True}, {"id": "row-10", "ok": False, "error": "Cannot delete"},
            ]})
    await page.route("**/api/v1/mcp-proposals/batch", failure)
    await page.get_by_role("button", name="批量删除", exact=True).click()
    confirmation = page.get_by_role("dialog", name="确认操作", exact=True)
    await confirmation.get_by_role("button", name="确认执行", exact=True).click()
    if outcome == "http_error":
        await expect(confirmation.get_by_role("alert")).to_contain_text("Temporary batch failure")
        await confirmation.get_by_role("button", name="关闭", exact=True).click()
    else:
        result = page.get_by_role("dialog", name="批量操作结果", exact=True)
        await expect(result).to_contain_text("Cannot delete")
        await result.get_by_role("button", name="关闭", exact=True).click()
    await expect(page.get_by_text("已选 2 项", exact=True)).to_be_visible()
    await expect(page.locator('tr[data-row-id="row-10"] input')).to_be_checked()
    await page.get_by_role("button", name="上一页", exact=True).click()
    await expect(page.locator('tr[data-row-id="row-0"] input')).to_be_checked()


async def test_admin_personal_capabilities_show_own_authentication_and_user_totals(console_page):
    page, url = console_page
    rows = [service(i) for i in range(4)]
    rows[0]["personal_credentials"] = {
        "current_user_configured": True, "configured_users": 3, "total_users": 4,
    }
    rows[1]["personal_credentials"] = {
        "current_user_configured": False, "configured_users": 2, "total_users": 4,
    }
    rows[3].update(cache_status="error", tool_count=0, resource_count=0, prompt_count=0, template_count=0)
    rows[3]["personal_credentials"] = {
        "current_user_configured": True, "configured_users": 3, "total_users": 4,
    }
    await mock_records(page, "/mcps", rows, [])
    await page.goto(url + "/#/mcps")
    own = page.locator('tr[data-row-id="row-0"] .service-capabilities')
    await expect(own).to_contain_text("当前用户能力列表")
    await expect(own).to_contain_text("已认证 3 / 4")
    labels = own.locator(".capability-summary > span")
    await expect(labels).to_have_text(["工具 2", "资源 3", "提示词 4", "资源模板 5"])
    positions = await labels.evaluate_all("nodes => nodes.map(n => n.getBoundingClientRect().top)")
    assert positions == sorted(set(positions)), positions
    missing = page.locator('tr[data-row-id="row-1"] .service-capabilities')
    await expect(missing).to_contain_text("当前用户未认证")
    await expect(missing).to_contain_text("已认证 2 / 4")
    await expect(missing.locator(".capability-summary")).to_have_count(0)
    await expect(page.locator('tr[data-row-id="row-2"] .capability-summary')).to_contain_text("工具 2")
    failed = page.locator('tr[data-row-id="row-3"] .service-capabilities')
    await expect(failed).to_contain_text("当前用户能力列表")
    await expect(failed.locator(".capability-summary > span")).to_have_text(
        ["工具 0", "资源 0", "提示词 0", "资源模板 0"],
    )
    await expect(failed).to_contain_text("已认证 3 / 4")
