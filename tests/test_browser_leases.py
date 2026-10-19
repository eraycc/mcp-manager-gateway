"""Browser acceptance for connection management and refresh selection."""
import re

import pytest
import test_browser_bug1 as browser_fixtures
from playwright.async_api import expect

leases_browser = browser_fixtures.bug1_browser


@pytest.mark.asyncio
async def test_connections_confirm_details_batch_and_refresh(leases_browser):
    page, url = leases_browser
    response = await page.request.get(url + "/api/v1/me")
    actor = (await response.json())["id"]
    rows = [{
        "id": "lease-" + str(i), "lease_id": "lease-" + str(i),
        "user_id": actor if i == 1 else "other-user", "username": "admin" if i == 1 else "alice",
        "token_name": "desktop", "kind": "retention", "status": "idle",
        "created_at": "2026-10-19T00:00:00Z", "touched_at": "2026-10-19T01:00:00Z",
        "expires_in": None, "instances": [], "can_disconnect": True, "idle_seconds": 1,
    } for i in (1, 2)]
    requests = []
    async def listing(route):
        requests.append(route.request.url)
        await route.fulfill(json={
            "items": rows.copy(), "total": len(rows), "page_size": 20,
            "visible_ids": [x["id"] for x in rows],
            "mcp_refs": [{"mcp_id": "referenced", "mcp_name": "Referenced MCP", "ref_count": 1}],
            "user_refs": [{"user_id": actor, "username": "admin"}, {"user_id": "other-user", "username": "alice"}],
        })
    await page.route("**/api/v1/leases?*", listing)
    await page.route("**/api/v1/leases/lease-1", lambda r: r.fulfill(json={**rows[0], "shared_leases": []}))
    calls = []
    async def disconnect(route):
        calls.append(route.request.post_data_json if route.request.url.endswith("/batch") else "single")
        if route.request.url.endswith("/batch"):
            rows.clear()
            await route.fulfill(json={"results": [{"lease_id": "lease-2", "ok": True}], "summary": {"total": 1, "ok": 1, "failed": 0}})
        else:
            rows.pop(0)
            await route.fulfill(json={"ok": True, "released": True, "instances": [], "session_cleanup_ok": True})
    await page.route(re.compile(r".*/api/v1/leases/(lease-\d+/disconnect|batch)$"), disconnect)
    await page.goto(url + "/#/leases")
    await expect(page.get_by_role("heading", name="连接管理", exact=True)).to_be_visible()
    links = await page.locator("nav a").all_text_contents()
    assert links.index("连接管理") < links.index("调用日志")
    await expect(page.get_by_label("MCP", exact=True).locator("option")).to_have_count(2)
    first = page.locator('tr[data-row-id="lease-1"]')
    await first.get_by_role("button", name="详情", exact=True).click()
    await expect(page.get_by_role("dialog")).to_contain_text("shared_leases")
    await page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
    await first.get_by_role("button", name="断开", exact=True).click()
    await page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
    assert not calls
    await first.get_by_role("checkbox").check()
    await page.get_by_label("搜索", exact=True).fill("draft")
    count = len(requests)
    await page.wait_for_event("response", predicate=lambda r: "/api/v1/leases?" in r.url, timeout=8000)
    assert len(requests) > count
    await expect(first.get_by_role("checkbox")).to_be_checked()
    await expect(page.get_by_label("搜索", exact=True)).to_have_value("draft")
    await first.get_by_role("button", name="断开", exact=True).click()
    await page.get_by_role("dialog").get_by_role("button", name="确认执行", exact=True).click()
    await expect(first).to_have_count(0)
    await page.get_by_role("dialog", name="断开结果").get_by_role("button", name="关闭", exact=True).click()
    second = page.locator('tr[data-row-id="lease-2"]')
    await second.get_by_role("checkbox").check()
    await page.get_by_role("button", name="批量断开", exact=True).click()
    await expect(page.get_by_role("dialog", name="确认操作")).to_contain_text("他人")
    await page.get_by_role("dialog", name="确认操作").get_by_role("button", name="确认执行").click()
    await expect(page.get_by_role("dialog", name="断开结果")).to_be_visible()
    assert calls == ["single", {"lease_ids": ["lease-2"]}]
    await page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.asyncio
async def test_connection_poll_pause_hidden_selection_pruning_and_page_abort(leases_browser):
    page, url = leases_browser
    await page.clock.install()
    rows = [{"id": "live", "username": "alice", "user_id": "other", "kind": "retention",
             "status": "idle", "instances": [], "expires_in": None}]
    seen = []
    async def listing(route):
        seen.append(route.request.url)
        await route.fulfill(json={"items": rows.copy(), "total": len(rows),
                                 "visible_ids": [r["id"] for r in rows], "mcp_refs": [], "user_refs": []})
    await page.route("**/api/v1/leases?*", listing)
    await page.goto(url + "/#/leases")
    row = page.locator('tr[data-row-id="live"]')
    await expect(row).to_be_visible()
    await row.get_by_role("checkbox").check()
    await expect(page.get_by_text("已选 1 项", exact=True)).to_be_visible()
    await page.get_by_label("暂停自动刷新").check()
    count = len(seen)
    await page.clock.fast_forward(15000)
    assert len(seen) == count
    await page.get_by_role("button", name="刷新", exact=True).click()
    assert len(seen) == count + 1
    await page.get_by_label("暂停自动刷新").uncheck()
    await page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true})")
    await page.clock.fast_forward(15000)
    assert len(seen) == count + 1
    await page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false})")
    rows.clear()
    await page.clock.fast_forward(5000)
    await expect(row).to_have_count(0)
    await expect(page.get_by_text("已选 0 项", exact=True)).to_be_visible()
    await page.goto(url + "/#/tokens")
    await expect(page.get_by_role("heading", name="访问令牌", exact=True)).to_be_visible()
    count = len(seen)
    await page.clock.fast_forward(15000)
    assert len(seen) == count


@pytest.mark.asyncio
async def test_dynamic_filters_restore_url_and_survive_search(leases_browser):
    page, url = leases_browser
    async def listing(route):
        await route.fulfill(json={"items": [], "total": 0, "visible_ids": [],
                                 "mcp_refs": [{"mcp_id": "referenced", "mcp_name": "Referenced MCP"}],
                                 "user_refs": [{"user_id": "other", "username": "alice"}]})
    await page.route("**/api/v1/leases?*", listing)
    await page.goto(url + "/#/leases?mcp_id=referenced&user_id=other")
    await expect(page.get_by_label("MCP", exact=True)).to_have_value("referenced")
    await expect(page.get_by_label("用户", exact=True)).to_have_value("other")
    async with page.expect_response(lambda r: "/api/v1/leases?" in r.url and "q=echo" in r.url) as response:
        await page.get_by_label("搜索", exact=True).fill("echo")
        await page.get_by_role("button", name="搜索", exact=True).click()
    assert "mcp_id=referenced" in (await response.value).url
    await expect(page.get_by_label("MCP", exact=True)).to_have_value("referenced")


@pytest.mark.asyncio
async def test_batch_network_failure_keeps_confirmed_results_without_replay(leases_browser):
    from urllib.parse import parse_qs, urlparse
    page, url = leases_browser
    rows = [{"id": "lease-" + str(i), "user_id": "other", "username": "alice",
             "kind": "retention", "status": "idle", "instances": []} for i in range(201)]
    async def listing(route):
        query = parse_qs(urlparse(route.request.url).query)
        size = int(query.get("page_size", ["20"])[0])
        page_number = int(query.get("page", ["1"])[0])
        await route.fulfill(json={"items": rows[(page_number-1)*size:page_number*size],
                                 "total": 201, "page_size": size, "visible_ids": [r["id"] for r in rows],
                                 "mcp_refs": [], "user_refs": [{"user_id": "other", "username": "alice"}]})
    calls = []
    async def batch(route):
        ids = route.request.post_data_json["lease_ids"]
        calls.append(ids)
        if len(calls) == 1:
            await route.fulfill(json={"results": [{"lease_id": x, "ok": True} for x in ids]})
        else:
            await route.abort("failed")
    await page.route("**/api/v1/leases?*", listing)
    await page.route("**/api/v1/leases/batch", batch)
    await page.goto(url + "/#/leases")
    await expect(page.get_by_role("heading", name="连接管理", exact=True)).to_be_visible()
    await page.get_by_role("button", name="选择全部筛选结果", exact=True).click()
    await expect(page.get_by_text("已选 201 项", exact=True)).to_be_visible()
    await page.get_by_role("button", name="批量断开", exact=True).click()
    await page.get_by_role("dialog", name="确认操作").get_by_role("button", name="确认执行").click()
    result = page.get_by_role("dialog", name="断开结果")
    await expect(result).to_contain_text("outcome_unknown")
    import json
    data = json.loads(await result.locator("pre").inner_text())
    assert len(data) == 201 and sum(x["ok"] for x in data) == 200
    assert len(calls) == 2
    await expect(page.get_by_role("dialog", name="确认操作")).to_have_count(0)


@pytest.mark.asyncio
async def test_manual_refresh_waits_for_older_poll_then_fetches_fresh_snapshot(leases_browser):
    import asyncio
    page, url = leases_browser
    await page.clock.install()
    entered, release = asyncio.Event(), asyncio.Event()
    version, calls = [1], []
    async def listing(route):
        captured = version[0]
        calls.append(captured)
        if len(calls) == 2:
            entered.set()
            await release.wait()
        await route.fulfill(json={"items": [{"id": "row", "username": "version-" + str(captured),
                                            "kind": "retention", "instances": []}], "total": 1,
                                 "visible_ids": ["row"], "mcp_refs": [], "user_refs": []})
    await page.route("**/api/v1/leases?*", listing)
    await page.goto(url + "/#/leases")
    await expect(page.locator("tbody")).to_contain_text("version-1")
    await page.clock.fast_forward(5000)
    await asyncio.wait_for(entered.wait(), 5)
    await page.get_by_label("暂停自动刷新").check()
    version[0] = 2
    await page.get_by_role("button", name="刷新", exact=True).click(no_wait_after=True)
    release.set()
    await expect(page.locator("tbody")).to_contain_text("version-2")
    assert calls == [1, 1, 2]
