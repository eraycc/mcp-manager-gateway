"""OAuth recovery stays separate from disconnect and uses current configuration."""
import asyncio

import pytest
import test_browser_runtime_requirements as browser_fixtures
from playwright.async_api import expect

runtime_browser = browser_fixtures.runtime_browser
local_update_check = browser_fixtures.local_update_check

RECOVERY = "已清理未完成的授权流程，请关闭所有旧授权标签页，再重新发起授权。"


@pytest.mark.parametrize("return_to,isolation,can_manage", [("mcps", "service", True), ("profile", "user", False)])
@pytest.mark.parametrize("width", [320, 1280])
async def test_reset_requires_confirmation_preserves_authorization_and_shows_stable_guidance(
    runtime_browser, return_to, isolation, can_manage, width
):
    page, _url = runtime_browser
    await page.set_viewport_size({"width": width, "height": 900})
    calls = {"status": 0, "reset": 0, "disconnect": 0}

    async def status(route):
        calls["status"] += 1
        await route.fulfill(json={"isolation": isolation, "config_isolation": "shared", "authorized": True})

    async def reset(route):
        assert route.request.method == "POST"
        calls["reset"] += 1
        await route.fulfill(json={"cleared_states": 2, "cleared_client": True, "message": RECOVERY})

    async def disconnect(route):
        calls["disconnect"] += 1
        await route.fulfill(json={"ok": True})

    await page.route("**/api/v1/mcps/demo/oauth/status", status)
    await page.route("**/api/v1/mcps/demo/oauth/reset", reset)
    await page.route("**/api/v1/mcps/demo/oauth/disconnect", disconnect)
    await page.route("**/api/v1/mcps/demo/credentials/status", lambda r: r.fulfill(json={
        "auth_type": "oauth", "config_isolation": "shared", "revision": 2,
    }))
    await page.evaluate("""async args=>{
      const {oauthDialog}=await import('/mcps.js');
      await oauthDialog({id:'demo',revision:1},args.canManage,()=>{},args.returnTo);
    }""", {"canManage": can_manage, "returnTo": return_to})
    popup = page.get_by_role("dialog", name="授权服务", exact=True)
    reset_button = popup.get_by_role("button", name="重置授权", exact=True)
    await expect(reset_button).to_be_visible()
    await expect(reset_button).not_to_have_class("danger")
    await expect(reset_button).not_to_have_class("primary")
    await expect(popup.get_by_role("button", name="断开 OAuth 授权", exact=True)).to_have_class("danger")
    await reset_button.focus()
    await reset_button.press("Enter")
    confirmation = page.get_by_role("dialog", name="重置授权", exact=True)
    await expect(confirmation).to_contain_text("不会撤销已发放的 token")
    assert calls["reset"] == 0
    await confirmation.get_by_role("button", name="关闭", exact=True).click()
    assert calls["reset"] == calls["disconnect"] == 0
    await reset_button.click()
    async with page.expect_response("**/api/v1/mcps/demo/oauth/reset") as pending:
        await page.get_by_role("dialog", name="重置授权", exact=True).get_by_role(
            "button", name="确认重置", exact=True
        ).click()
    assert (await pending.value).status == 200
    await expect(popup.get_by_role("status")).to_have_text(RECOVERY)
    await expect(popup).to_contain_text("当前登录用户已完成 OAuth 授权")
    assert calls["reset"] == 1 and calls["disconnect"] == 0
    assert calls["status"] >= 2
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert await popup.evaluate("(n)=>n.scrollWidth<=n.clientWidth+1")
    if width <= 390:
        assert (await reset_button.bounding_box())["height"] >= 44


async def test_shared_non_admin_cannot_reset_service_authorization(runtime_browser):
    page, _url = runtime_browser
    await page.route("**/api/v1/mcps/demo/oauth/status", lambda r: r.fulfill(
        json={"isolation": "service", "authorized": False}))
    await page.evaluate("""async()=>{
      const {oauthDialog}=await import('/mcps.js');
      await oauthDialog({id:'demo'},false);
    }""")
    await expect(page.get_by_role("button", name="重置授权", exact=True)).to_have_count(0)


async def test_personal_oauth_editor_opens_latest_config_and_saves_current_revision(runtime_browser):
    page, _url = runtime_browser
    version = {"revision": 2, "client": "first-current-client"}
    reads = []

    async def status(route):
        reads.append(version["revision"])
        await route.fulfill(json={
            "auth_type": "oauth", "config_isolation": "user", "revision": version["revision"],
            "oauth_config": {"type": "oauth", "auth_mode": "pre_registration",
                             "client_id": version["client"], "authorization_url": "https://auth.example/authorize",
                             "token_url": "https://auth.example/token", "scopes": ["read"]},
            "refresh_supported": True, "refresh_allowed": True,
        })

    await page.route("**/api/v1/mcps/demo/credentials/status", status)
    await page.route("**/api/v1/mcps/demo/credentials", lambda r: r.fulfill(json={"state": "pending_authorization"}))
    await page.route("**/api/v1/mcps/demo/credentials/refresh", lambda r: r.fulfill(json={
        "revision": version["revision"], "refresh_supported": True, "refresh_allowed": True,
        "tool_count": 1, "resource_count": 0, "prompt_count": 0, "template_count": 0,
    }))
    open_editor = """async()=>{
      const {credentialDialog}=await import('/mcps.js');
      await credentialDialog({id:'demo',transport:'streamable-http',revision:1});
    }"""
    await page.evaluate(open_editor)
    popup = page.get_by_role("dialog", name="OAuth 配置", exact=True)
    await expect(popup.get_by_label("Client ID", exact=True)).to_have_value("first-current-client")
    await expect(popup.get_by_role("button", name="刷新能力缓存", exact=True)).to_be_enabled()
    async with page.expect_response("**/api/v1/mcps/demo/credentials/refresh") as refreshed:
        await popup.get_by_role("button", name="刷新能力缓存", exact=True).click()
    assert (await refreshed.value).request.post_data_json["revision"] == 2
    await popup.get_by_role("button", name="关闭", exact=True).click()
    version.update(revision=3, client="latest-personal-client")
    await page.evaluate(open_editor)
    await expect(popup.get_by_label("Client ID", exact=True)).to_have_value("latest-personal-client")
    async with page.expect_response(lambda r: r.url.endswith("/mcps/demo/credentials") and r.request.method == "PUT") as pending:
        await popup.get_by_role("button", name="保存", exact=True).click()
    response = await pending.value
    assert response.request.post_data_json["revision"] == 3
    assert response.request.post_data_json["auth"]["client_id"] == "latest-personal-client"
    assert reads == [2, 3]


async def test_profile_shared_config_summary_reads_fresh_without_personal_form(runtime_browser):
    page, _url = runtime_browser
    current = {"client_id": "latest-shared-client", "auth_mode": "pre_registration", "scopes": ["read", "write"]}
    await page.route("**/api/v1/mcps/demo/oauth/status", lambda r: r.fulfill(
        json={"isolation": "user", "authorized": False}))
    await page.route("**/api/v1/mcps/demo/credentials/status", lambda r: r.fulfill(json={
        "auth_type": "oauth", "config_isolation": "shared", "revision": 7,
        "oauth_config_summary": current,
    }))
    open_authorization = """async()=>{
      const {oauthDialog}=await import('/mcps.js');
      await oauthDialog({id:'demo',revision:1,config:{auth:{client_id:'old-list-client'}}},false,()=>{},'profile');
    }"""
    await page.evaluate(open_authorization)
    popup = page.get_by_role("dialog", name="授权服务", exact=True)
    await expect(popup).to_contain_text("共享配置由管理员维护")
    await expect(popup).to_contain_text("latest-shared-client")
    await expect(popup).not_to_contain_text("old-list-client")
    await expect(popup.locator("input,select,textarea")).to_have_count(0)
    await popup.get_by_role("button", name="关闭", exact=True).click()
    current["client_id"] = "newest-shared-client"
    await page.evaluate(open_authorization)
    await expect(popup).to_contain_text("newest-shared-client")
    await expect(popup).not_to_contain_text("latest-shared-client")


async def test_outdated_personal_entry_respects_current_shared_config(runtime_browser):
    page, _url = runtime_browser
    await page.route("**/api/v1/mcps/demo/credentials/status", lambda r: r.fulfill(json={
        "auth_type": "oauth", "config_isolation": "shared", "revision": 7,
        "oauth_config_summary": {"client_id": "current-shared-client", "client_secret": "never-render-secret"},
    }))
    await page.evaluate("""async()=>{
      const {credentialDialog}=await import('/mcps.js');
      await credentialDialog({id:'demo',transport:'streamable-http',revision:1});
    }""")
    popup = page.get_by_role("dialog", name="OAuth 配置", exact=True)
    await expect(popup).to_contain_text("current-shared-client")
    await expect(popup).to_contain_text("共享配置由管理员维护")
    await expect(popup).not_to_contain_text("never-render-secret")
    await expect(popup.locator("input,select,textarea")).to_have_count(0)
    await expect(popup.get_by_role("button", name="保存", exact=True)).to_have_count(0)


async def test_failed_reset_keeps_confirmation_error_and_authorization(runtime_browser):
    page, _url = runtime_browser
    await page.route("**/api/v1/mcps/demo/oauth/status", lambda r: r.fulfill(
        json={"isolation": "user", "authorized": True}))
    await page.route("**/api/v1/mcps/demo/oauth/reset", lambda r: r.fulfill(
        status=409, json={"detail": "授权配置正在更新，请稍后重试"}))
    await page.evaluate("""async()=>{
      const {oauthDialog}=await import('/mcps.js');
      await oauthDialog({id:'demo'},false);
    }""")
    popup = page.get_by_role("dialog", name="授权服务", exact=True)
    await popup.get_by_role("button", name="重置授权", exact=True).click()
    confirmation = page.get_by_role("dialog", name="重置授权", exact=True)
    async with page.expect_response("**/api/v1/mcps/demo/oauth/reset") as pending:
        await confirmation.get_by_role("button", name="确认重置", exact=True).click()
    assert (await pending.value).status == 409
    await expect(confirmation.get_by_role("alert")).to_contain_text("授权配置正在更新")
    await expect(popup.get_by_role("status")).not_to_be_visible()
    await confirmation.get_by_role("button", name="关闭", exact=True).click()
    await expect(popup).to_contain_text("当前登录用户已完成 OAuth 授权")


async def test_new_authorization_replaces_previous_link_after_delayed_response(runtime_browser):
    page, _url = runtime_browser
    entered, release = asyncio.Event(), asyncio.Event()
    starts = []

    async def start(route):
        starts.append(route.request.method)
        if len(starts) == 2:
            entered.set()
            await release.wait()
        await route.fulfill(json={"authorization_url": "https://auth.example/flow/" + str(len(starts))})

    await page.route("**/api/v1/mcps/demo/oauth/status", lambda r: r.fulfill(
        json={"isolation": "user", "config_isolation": "shared", "authorized": False}))
    await page.route("**/api/v1/mcps/demo/oauth/start", start)
    await page.evaluate("""async()=>{
      const {oauthDialog}=await import('/mcps.js');
      await oauthDialog({id:'demo'},false);
    }""")
    popup = page.get_by_role("dialog", name="授权服务", exact=True)
    authorize = popup.get_by_role("button", name="开始授权", exact=True)
    links = popup.get_by_role("link", name="打开授权页面", exact=True)
    await authorize.click()
    await expect(links).to_have_attribute("href", "https://auth.example/flow/1")
    await authorize.click()
    await asyncio.wait_for(entered.wait(), 5)
    try:
        await expect(links).to_have_count(0)
    finally:
        release.set()
    await expect(authorize).to_be_enabled()
    await expect(links).to_have_count(1)
    await expect(links).to_have_attribute("href", "https://auth.example/flow/2")
    assert starts == ["POST", "POST"]


async def test_reset_invalidates_authorization_response_still_in_flight(runtime_browser):
    page, _url = runtime_browser
    entered, release = asyncio.Event(), asyncio.Event()

    async def start(route):
        entered.set()
        await release.wait()
        await route.fulfill(json={"authorization_url": "https://auth.example/obsolete"})

    await page.route("**/api/v1/mcps/demo/oauth/status", lambda r: r.fulfill(
        json={"isolation": "user", "config_isolation": "shared", "authorized": False}))
    await page.route("**/api/v1/mcps/demo/oauth/start", start)
    await page.route("**/api/v1/mcps/demo/oauth/reset", lambda r: r.fulfill(
        json={"cleared_states": 1, "cleared_client": True, "message": RECOVERY}))
    await page.evaluate("""async()=>{
      const {oauthDialog}=await import('/mcps.js');
      await oauthDialog({id:'demo'},false);
    }""")
    popup = page.get_by_role("dialog", name="授权服务", exact=True)
    authorize = popup.get_by_role("button", name="开始授权", exact=True)
    await authorize.click()
    await asyncio.wait_for(entered.wait(), 5)
    try:
        await popup.get_by_role("button", name="重置授权", exact=True).click()
        async with page.expect_response("**/api/v1/mcps/demo/oauth/reset") as pending:
            await page.get_by_role("dialog", name="重置授权", exact=True).get_by_role(
                "button", name="确认重置", exact=True
            ).click()
        assert (await pending.value).status == 200
        await expect(popup.get_by_role("status")).to_have_text(RECOVERY)
    finally:
        release.set()
    await expect(authorize).to_be_enabled()
    await expect(popup.get_by_role("link", name="打开授权页面", exact=True)).to_have_count(0)
    await expect(popup.get_by_role("status")).to_have_text(RECOVERY)


async def test_real_personal_oauth_status_exposes_reset_without_management_flag(runtime_browser):
    page, _url = runtime_browser
    service = await page.evaluate("""async()=>{
      const {api}=await import('/core.js');
      return await api('/mcps',{method:'POST',body:{
        name:'Personal OAuth reset',slug:'browser-personal-oauth-reset',
        transport:'streamable-http',mode:'disabled',isolation:'user',
        config:{url:'https://mcp.example.com/mcp',auth:{
          type:'oauth',auth_mode:'pre_registration',config_isolation:'shared',
          authorization_url:'https://auth.example.com/authorize',
          token_url:'https://auth.example.com/token',client_id:'browser-client'
        }}
      }});
    }""")
    async with page.expect_response("**/api/v1/mcps/" + service["id"] + "/oauth/status") as pending:
        await page.evaluate("""async service=>{
          const {oauthDialog}=await import('/mcps.js');
          await oauthDialog(service,false,()=>{},'profile');
        }""", service)
    response = await pending.value
    assert response.status == 200
    state = await response.json()
    assert state["isolation"] == "user" and "scope" not in state
    popup = page.get_by_role("dialog", name="授权服务", exact=True)
    await expect(popup.get_by_role("button", name="重置授权", exact=True)).to_be_visible()
    await expect(popup).to_contain_text("browser-client")
    await popup.get_by_role("button", name="重置授权", exact=True).click()
    async with page.expect_response("**/api/v1/mcps/" + service["id"] + "/oauth/reset") as reset:
        await page.get_by_role("dialog", name="重置授权", exact=True).get_by_role(
            "button", name="确认重置", exact=True
        ).click()
    assert (await reset.value).status == 200
    await expect(popup.get_by_role("status")).to_have_text(RECOVERY)
