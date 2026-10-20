"""UI acceptance for issue.log: coordinated menus and bounded lease cells."""
import pytest
import test_browser_bug1 as browser_fixtures
from playwright.async_api import expect

runtime_browser = browser_fixtures.bug1_browser


@pytest.fixture(autouse=True)
def local_update_check(monkeypatch):
    # These UI contracts do not depend on the public package registry.
    async def latest():
        from mcp_manager.updates import PYPI_PROJECT_URL, RELEASES_URL, VERSION
        return {"version": VERSION, "pypi_url": PYPI_PROJECT_URL, "releases_url": RELEASES_URL}
    monkeypatch.setattr("mcp_manager.updates.fetch_latest_release", latest)


@pytest.mark.asyncio
async def test_top_action_menus_are_exclusive_and_dismiss_outside(runtime_browser):
    page, _url = runtime_browser
    await page.evaluate("""async()=>{
      const {topActions}=await import('/shell-controls.js');
      document.querySelector('.top-actions').replaceWith(topActions({
        me:{username:'admin'},isAdmin:true,translationConfig:{enabled:true},
        onLogout:async()=>{},onRefresh:()=>{}
      }));
    }""")
    panels = page.locator(".top-actions details.action-menu[open]")
    for label in ("切换语言", "显示模式", "账户菜单"):
        await page.locator('summary[aria-label="'+label+'"]').click()
        await expect(panels).to_have_count(1)
        await expect(panels.locator("summary")).to_have_attribute("aria-label", label)
        await panels.locator(".action-menu-panel").click(position={"x": 8, "y": 8})
        await expect(panels).to_have_count(1)
    await page.get_by_role("heading", name="仪表盘", exact=True).click()
    await expect(panels).to_have_count(0)
    await page.locator('summary[aria-label="显示模式"]').click()
    await page.keyboard.press("Escape")
    await expect(panels).to_have_count(0)


@pytest.mark.asyncio
async def test_lease_instance_cells_show_three_rows_and_scroll_locally(runtime_browser, tmp_path):
    page, url = runtime_browser
    def lease(index, count):
        return {"id": str(index), "username": "alice", "kind": "retention", "status": "idle",
                "instances": [{"server_id": str(i), "name": "MCP " + str(i), "isolation": "service",
                               "phase": "ready", "lease_count": 1, "in_flight": 0} for i in range(count)]}
    rows = [lease(0, 0), lease(1, 1), lease(2, 2), lease(3, 3), lease(4, 4), lease(10, 10)]
    await page.route("**/api/v1/leases?*", lambda r: r.fulfill(json={
        "items": rows, "total": len(rows), "mcp_refs": [], "user_refs": [],
        "visible_ids": [x["id"] for x in rows],
    }))
    await page.goto(url + "/#/leases")
    cells = page.locator(".lease-instances")
    await expect(cells).to_have_count(6)
    metrics = await cells.evaluate_all("""nodes=>nodes.map(n=>({
      height:n.clientHeight,scroll:n.scrollHeight,overflow:getComputedStyle(n).overflowY,
      overscroll:getComputedStyle(n).overscrollBehaviorY
    }))""")
    assert metrics[0]["height"] <= metrics[1]["height"]
    assert metrics[1]["height"] < metrics[2]["height"] < metrics[3]["height"]
    assert metrics[3]["height"] == metrics[4]["height"] == metrics[5]["height"]
    assert metrics[3]["height"] <= 114
    for metric in metrics[:4]:
        assert metric["scroll"] <= metric["height"] + 1
    assert metrics[4]["scroll"] > metrics[4]["height"]
    assert metrics[5]["overscroll"] in {"contain", "none"}
    await expect(page.locator(".lease-instance-hint").filter(has_text="上下滚动查看")).to_have_count(2)
    heights = await page.locator(".lease-instance-cell").evaluate_all("nodes=>nodes.map(n=>n.clientHeight)")
    assert heights[3] == heights[4] == heights[5] <= 132
    row_heights = await cells.evaluate_all("nodes=>nodes.map(n=>n.closest(\'tr\').clientHeight)")
    assert row_heights[1] < row_heights[2] < row_heights[3]
    assert row_heights[3] == row_heights[4] == row_heights[5]
    capture = tmp_path / "lease-instance-layout.png"
    await expect(page.locator("#toast")).not_to_be_visible()
    await page.screenshot(path=str(capture), full_page=True)
    print("UI_CAPTURE " + str(capture))
    target = cells.last
    await target.focus()
    await page.keyboard.press("End")
    await expect(target.get_by_text("MCP 9", exact=True)).to_be_in_viewport()
    assert await target.evaluate("(n)=>n.scrollTop") > 0


@pytest.mark.asyncio
async def test_runtime_setting_distinguishes_session_and_instance_and_persists_zero(runtime_browser):
    page, url = runtime_browser
    await page.goto(url + "/#/settings?settings_tab=runtime")
    field = page.get_by_label("默认连接有效期（秒）", exact=True)
    # Follow the existing runtime tab's actual stable label.
    if not await field.is_visible():
        await page.get_by_role("tab", name="运行与日志", exact=True).click()
    await expect(field).to_have_value("43200")
    idle = page.get_by_label("默认空闲回收时间（秒）", exact=True)
    assert (await field.bounding_box())["y"] > (await idle.bounding_box())["y"]
    await expect(page.locator("#settings-hint-idle_seconds")).to_contain_text("MCP 服务实例")
    await expect(page.locator("#settings-hint-connection_ttl_seconds")).to_contain_text("连接/租期/会话")
    await expect(page.locator("#settings-hint-connection_ttl_seconds")).to_contain_text("0 表示不自动释放")
    heartbeat = page.get_by_label("Bridge 默认心跳间隔（秒）", exact=True)
    missed = page.get_by_label("Bridge 失联判定次数", exact=True)
    await expect(heartbeat).to_have_value("90")
    await expect(heartbeat).to_have_attribute("min", "1")
    await expect(page.locator("#settings-hint-bridge_heartbeat_seconds")).to_contain_text("必须为大于 0")
    await expect(page.locator("#settings-hint-bridge_heartbeat_seconds")).to_contain_text("仍按此间隔发送心跳")
    await expect(page.locator("#settings-hint-bridge_heartbeat_seconds")).to_contain_text("stdio")
    await expect(page.locator("#settings-hint-bridge_heartbeat_seconds")).to_contain_text("MMG")
    await expect(missed).to_have_value("3")
    await expect(missed).to_have_attribute("min", "0")
    await expect(page.locator("#settings-hint-bridge_missed_heartbeats")).to_contain_text("0 表示禁用")
    assert (await heartbeat.bounding_box())["y"] > (await field.bounding_box())["y"]
    assert (await missed.bounding_box())["y"] > (await heartbeat.bounding_box())["y"]
    await heartbeat.fill("60")
    await missed.fill("4")
    await field.fill("0")
    async with page.expect_response(lambda r: "/api/v1/settings" in r.url and r.request.method == "PATCH") as pending:
        await page.get_by_role("button", name="保存设置", exact=True).click()
    assert (await pending.value).status == 200
    await page.reload()
    await expect(page.get_by_label("默认连接有效期（秒）", exact=True)).to_have_value("0")
    await expect(page.get_by_label("Bridge 默认心跳间隔（秒）", exact=True)).to_have_value("60")
    await expect(page.get_by_label("Bridge 失联判定次数", exact=True)).to_have_value("4")
    await heartbeat.fill("0")
    assert not await heartbeat.evaluate("(n)=>n.checkValidity()")
    await heartbeat.fill("60")
    await missed.fill("0")
    async with page.expect_response(lambda r: "/api/v1/settings" in r.url and r.request.method == "PATCH") as pending:
        await page.get_by_role("button", name="保存设置", exact=True).click()
    assert (await pending.value).status == 200
    await page.reload()
    await expect(heartbeat).to_have_value("60")
    await expect(missed).to_have_value("0")
    await expect(page.locator("#settings-hint-bridge_missed_heartbeats")).to_contain_text("连接有效期独立生效")


@pytest.mark.asyncio
async def test_lease_status_distinguishes_current_and_accumulated_calls(runtime_browser, tmp_path):
    page, url = runtime_browser
    rows = [
        {"id": "quick", "username": "alice", "kind": "retention", "status": "idle",
         "in_flight": 0, "queued_calls": 0, "call_count": 5, "last_call_at": "2026-10-20T00:01:02+00:00",
         "instances": []},
        {"id": "waiting", "username": "bob", "kind": "bridge", "status": "active",
         "bridge_heartbeat_seconds": 60, "bridge_heartbeat_detection_enabled": False, "bridge_disconnect_at": None,
         "in_flight": 0, "queued_calls": 1, "call_count": 0, "last_call_at": None, "instances": []},
    ]
    await page.route("**/api/v1/leases?*", lambda r: r.fulfill(json={
        "items": rows, "total": len(rows), "visible_ids": [r["id"] for r in rows],
        "mcp_refs": [], "user_refs": [],
    }))
    await page.goto(url + "/#/leases")
    await expect(page.locator(".lease-activity")).to_have_count(2)
    completed = page.locator(".lease-activity").first
    await expect(completed).to_contain_text("当前执行 0")
    await expect(completed).to_contain_text("累计下游调用 5 次")
    await expect(completed).to_contain_text("最近调用")
    waiting = page.locator(".lease-activity").last
    await expect(waiting).to_contain_text("等待派发 1")
    await expect(waiting).to_contain_text("尚无下游调用")
    await expect(page.locator("tbody tr").filter(has_text="bob")).to_contain_text("Bridge 失联检测已禁用")
    await completed.scroll_into_view_if_needed()
    capture = tmp_path / "lease-call-status.png"
    await expect(page.locator("#toast")).not_to_be_visible()
    await page.screenshot(path=str(capture), full_page=True)
    print("UI_CAPTURE " + str(capture))
