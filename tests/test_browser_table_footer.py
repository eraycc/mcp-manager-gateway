"""Browser regressions for table pagination across reload and empty states."""
import asyncio

import pytest
from playwright.async_api import async_playwright, expect

from test_gateway import running_gateway  # noqa: F401


@pytest.fixture
async def table_browser(running_gateway):  # noqa: F811
    _, _, url, _, _ = running_gateway
    initial = {
        "items": [{"id": "last", "name": "Last record"}],
        "total": 41,
        "page": 3,
        "page_size": 20,
        "total_pages": 3,
    }
    state = {"data": initial, "status": 200, "hold": False}
    started, release = asyncio.Event(), asyncio.Event()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",
        )
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.clock.install()
        await page.route(
            "**/table-regression",
            lambda route: route.fulfill(
                content_type="text/html",
                body='<main id="records"></main><div id="toast"></div>',
            ),
        )

        async def records(route):
            if state["hold"]:
                started.set()
                await release.wait()
            await route.fulfill(status=state["status"], json=state["data"])

        await page.route("**/api/v1/footer-records?*", records)
        await page.goto(url + "/table-regression#/records?page=3")
        await page.evaluate("""async () => {
            const {tablePage} = await import('/core.js');
            window.tableAbort = new AbortController();
            window.reloadTable = await tablePage(document.querySelector('main'), {
                path: '/footer-records',
                columns: [['Name', 'name']],
                signal: window.tableAbort.signal,
                bulk: [['Apply change', async () => {}]],
            });
        }""")
        await expect(page.locator(".pagination")).to_contain_text("3 / 3")
        try:
            yield page, state, started, release
            assert errors == []
        finally:
            release.set()
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["empty", "error"])
async def test_bulk_reload_removes_previous_pagination(table_browser, outcome):
    page, state, started, release = table_browser
    state["hold"] = True
    if outcome == "empty":
        state["data"] = {
            "items": [], "total": 0, "page": 1, "page_size": 20, "total_pages": 1,
        }
    else:
        state.update(status=503, data={"detail": "Temporary reload failure"})

    await page.get_by_role("checkbox", name="选择 Last record", exact=True).check()
    await page.get_by_role("button", name="Apply change", exact=True).click()
    await asyncio.wait_for(started.wait(), 5)
    await expect(page.get_by_role("status")).to_contain_text("正在加载")
    footer = page.locator(".pagination")
    await expect(footer.locator("button, input, select")).to_have_count(0)
    await expect(footer).not_to_contain_text("41")
    await expect(footer).not_to_contain_text("3 / 3")

    release.set()
    if outcome == "empty":
        await expect(page.get_by_text("没有匹配记录，调整筛选条件或创建第一条记录。")).to_be_visible()
        await expect(footer).to_have_text("共 0 条")
        await expect(page.locator("table")).to_have_count(0)
    else:
        await expect(page.get_by_text("Temporary reload failure")).to_be_visible()
        await expect(page.get_by_role("button", name="重试", exact=True)).to_be_visible()
        await expect(footer).to_be_empty()
        # A successful retry must rebuild the controls for the newly loaded data.
        state.update(hold=False, status=200, data={
            "items": [{"id": "last", "name": "Restored record"}],
            "total": 41, "page": 3, "page_size": 20, "total_pages": 3,
        })
        await page.get_by_role("button", name="重试", exact=True).click()
        await expect(page.get_by_role("cell", name="Restored record", exact=True)).to_be_visible()
        await expect(footer).to_contain_text("3 / 3")
        await expect(page.get_by_role("button", name="上一页", exact=True)).to_be_enabled()
        await expect(page.get_by_role("button", name="下一页", exact=True)).to_be_disabled()


@pytest.mark.asyncio
async def test_silent_poll_renders_empty_result_without_old_controls(table_browser):
    page, state, _, _ = table_browser
    state["data"] = {
        "items": [], "total": 0, "page": 1, "page_size": 20, "total_pages": 1,
    }
    await page.clock.fast_forward(11000)
    await expect(page.get_by_text("没有匹配记录，调整筛选条件或创建第一条记录。")).to_be_visible()
    await expect(page.locator(".pagination")).to_have_text("共 0 条")
    await expect(page.locator(".pagination button, .pagination input, .pagination select")).to_have_count(0)
    await expect(page.locator("table")).to_have_count(0)


@pytest.mark.asyncio
async def test_nonempty_poll_keeps_rows_and_pagination_navigation(table_browser):
    page, state, _, _ = table_browser
    await page.evaluate("window.previousRow = document.querySelector('tbody tr')")
    state["data"] = {
        "items": [{"id": "last", "name": "Updated record"}],
        "total": 61, "page": 3, "page_size": 20, "total_pages": 4,
    }
    await page.clock.fast_forward(11000)
    await expect(page.get_by_role("cell", name="Updated record", exact=True)).to_be_visible()
    assert await page.evaluate("window.previousRow === document.querySelector('tbody tr')")
    await expect(page.locator(".pagination")).to_contain_text("共 61 条")
    await expect(page.locator(".pagination")).to_contain_text("3 / 4")
    await expect(page.get_by_role("button", name="下一页", exact=True)).to_be_enabled()
    await page.get_by_role("button", name="下一页", exact=True).click()
    assert page.url.endswith("#/records?page=4")


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["reload", "poll"])
async def test_empty_last_page_offers_navigation_without_automatic_route_change(table_browser, trigger):
    page, state, _, _ = table_browser
    state["data"] = {
        "items": [], "total": 5, "page": 3, "page_size": 20, "total_pages": 1,
    }
    await page.evaluate("""async () => {
        const {dialog} = await import('/core.js');
        dialog('Keep open', 'An existing dialog must survive the background update.');
    }""")
    if trigger == "reload":
        await page.evaluate("window.reloadTable()")
    else:
        await page.clock.fast_forward(11000)
    await expect(page.locator(".pagination")).to_contain_text("共 5 条")
    assert page.url.endswith("#/records?page=3")
    await expect(page.get_by_role("dialog", name="Keep open", exact=True)).to_be_visible()
    await page.get_by_role("dialog", name="Keep open", exact=True).get_by_role("button", name="关闭").click()
    return_button = page.get_by_role("button", name="返回第 1 页", exact=True)
    await expect(return_button).to_be_visible()
    await return_button.click()
    assert page.url.endswith("#/records?page=1")
