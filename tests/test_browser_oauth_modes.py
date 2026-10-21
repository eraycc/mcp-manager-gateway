"""One OAuth editor is shared by server, proposal and personal credential forms."""
import test_browser_runtime_requirements as browser_fixtures
from playwright.async_api import expect

runtime_browser = browser_fixtures.runtime_browser
local_update_check = browser_fixtures.local_update_check


async def test_oauth_mode_clears_hidden_credentials_and_secret_is_optional(runtime_browser):
    page, _ = runtime_browser
    await page.evaluate("""async()=>{
      const {oauthFields}=await import('/oauth-fields.js');
      window.oauthForm=oauthFields({client_id:'old-client',client_secret:'old-secret',type:'oauth'});
      document.querySelector('main').replaceChildren(oauthForm.node);
    }""")
    mode = page.get_by_label("OAuth 客户端注册方式", exact=True)
    identity = page.get_by_label("Client ID", exact=True)
    secret = page.get_by_label("Client Secret", exact=True)
    name = page.get_by_label("客户端名称（client_name）", exact=True)
    callback = page.get_by_label("OAuth 回调路径", exact=True)
    await expect(callback).to_have_value("/api/v1/oauth/callback")
    await expect(page.get_by_text("OAuth 回调路径。大多数服务使用默认的 /api/v1/oauth/callback；Figma 等部分远程 MCP 服务要求使用 /callback，若授权时提示 invalid_redirect_uri，请切换到 /callback 重试。", exact=True)).to_be_visible()
    await callback.select_option("/callback")
    scopes = page.get_by_label("Scopes（空格分隔）", exact=True)
    await expect(scopes).to_have_value("")
    hint = page.locator(".oauth-fields small").filter(has_text="用于声明申请的权限范围")
    await expect(hint).to_have_text("用于声明申请的权限范围，多个权限用空格分隔（如 read write mcp:connect）。留空时由服务器发现信息自动确定；Figma 等远程 MCP 通常填 mcp:connect。")
    await expect(hint).to_be_visible()
    await expect(hint.locator("br")).to_have_count(1)
    await expect(mode).to_have_value("pre_registration")
    await expect(identity).to_have_attribute("required", "")
    assert not await secret.evaluate("(n)=>n.required")
    await mode.select_option("prefer_cimd_fallback_dcr")
    await expect(identity).not_to_be_visible()
    await expect(identity).to_have_value("")
    await expect(secret).to_have_value("")
    await expect(page.get_by_role("checkbox")).to_have_count(0)
    await expect(name).to_be_visible()
    await expect(name).to_have_value("Codex")
    automatic = await page.evaluate("oauthForm.value()")
    assert automatic["auth_mode"] == "prefer_cimd_fallback_dcr" and automatic["client_name"] == "Codex"
    assert "allow_dcr" not in automatic
    assert automatic["callback_path"] == "/callback"
    assert "client_id" not in automatic and "client_secret" not in automatic
    await name.fill("My MMG")
    await mode.select_option("pre_registration")
    await expect(name).not_to_be_visible()
    await expect(name).to_be_disabled()
    await identity.fill("public-client")
    await scopes.fill("mcp:connect profile:read")
    manual = await page.evaluate("oauthForm.value()")
    assert manual["scopes"] == ["mcp:connect", "profile:read"]
    assert manual["client_id"] == "public-client"
    assert manual["callback_path"] == "/callback"
    await callback.select_option("/api/v1/oauth/callback")
    assert "callback_path" not in await page.evaluate("oauthForm.value()")
    assert "client_secret" not in manual and "client_name" not in manual and "allow_dcr" not in manual


async def test_automatic_registration_exposes_and_serializes_token_auth_method(runtime_browser):
    page, _ = runtime_browser
    await page.evaluate("""async()=>{
      const {oauthFields}=await import('/oauth-fields.js');
      window.oauthForm=oauthFields({type:'oauth',auth_mode:'auto',allow_dcr:true,client_name:'Codex',
                                   token_endpoint_auth_method:'client_secret_post',callback_path:'/callback'});
      document.querySelector('main').replaceChildren(oauthForm.node);
    }""")
    method = page.get_by_label("Token 端点认证方式", exact=True)
    await expect(method).to_be_visible()
    await expect(method).to_be_enabled()
    await expect(method).to_have_value("client_secret_post")
    await expect(page.get_by_label("OAuth 回调路径", exact=True)).to_have_value("/callback")
    for chosen in ("client_secret_basic", "none", ""):
        await method.select_option(chosen)
        value = await page.evaluate("oauthForm.value()")
        assert value.get("token_endpoint_auth_method") == (chosen or None)
        assert "client_id" not in value and "client_secret" not in value


async def test_token_method_is_submitted_by_server_proposal_and_personal_forms(runtime_browser):
    page, _ = runtime_browser
    submitted = []
    model = {"id": "method-test", "name": "Method test", "slug": "method-test", "transport": "streamable-http",
             "mode": "lazy", "isolation": "user", "revision": 1, "config": {"url": "https://mcp.test/mcp",
             "auth": {"type": "oauth", "auth_mode": "auto", "allow_dcr": True, "client_name": "Codex",
                      "token_endpoint_auth_method": "client_secret_post", "config_isolation": "user",
                      "callback_path": "/callback"}}}

    async def respond(route):
        path = route.request.url.split("/api/v1/", 1)[1]
        if route.request.method in {"POST", "PATCH", "PUT"}:
            submitted.append((path, route.request.post_data_json))
            await route.fulfill(json={"id": "method-test", "state": "pending_authorization"})
        elif path.endswith("/credentials/status"):
            await route.fulfill(json={"auth_type": "oauth", "required": True, "config_isolation": "user"})
        elif path.startswith("mcp-proposals/"):
            await route.fulfill(json={"id": "method-test", "payload": model, "mode": "lazy", "isolation": "user",
                                     "config_isolation": "user"})
        else:
            await route.fulfill(json=model)

    await page.route("**/api/v1/mcps/method-test**", respond)
    await page.route("**/api/v1/mcp-proposals/method-test**", respond)
    await page.route("**/api/v1/mcps", respond)
    for kind in ("create", "server", "proposal", "personal"):
        await page.evaluate("""async(kind)=>{
          const m=await import('/mcps.js');
          const service={id:'method-test',name:'Method test',transport:'streamable-http',revision:1};
          if(kind==='create')await m.mcpEditor();
          else if(kind==='server')await m.mcpEditor(service);
          else if(kind==='proposal')await m.proposalEditor(service);
          else await m.credentialDialog(service);
        }""", kind)
        dialog = page.get_by_role("dialog")
        if kind == "create":
            await dialog.get_by_label("服务名称", exact=True).fill("New OAuth")
            await dialog.get_by_label("传输协议", exact=True).select_option("streamable-http")
            await dialog.get_by_label("服务 URL", exact=True).fill("https://mcp.test/mcp")
            await dialog.get_by_label("认证方式", exact=True).select_option("oauth")
            await expect(dialog.get_by_label("OAuth 客户端注册方式", exact=True)).to_have_value("prefer_cimd_fallback_dcr")
        if kind == "personal":
            await dialog.get_by_label("OAuth 客户端注册方式", exact=True).select_option("prefer_cimd_fallback_dcr")
        method = dialog.get_by_label("Token 端点认证方式", exact=True)
        await expect(method).to_be_visible()
        await expect(method).to_be_enabled()
        if kind in {"server", "proposal"}:
            await expect(method).to_have_value("client_secret_post")
        await method.select_option("client_secret_basic")
        callback = dialog.get_by_label("OAuth 回调路径", exact=True)
        await expect(callback).to_have_value("/api/v1/oauth/callback" if kind in {"personal", "create"} else "/callback")
        await callback.select_option("/callback")
        label = {"create": "创建服务", "server": "保存修改", "proposal": "保存审批配置", "personal": "保存"}[kind]
        await dialog.get_by_role("button", name=label, exact=True).click()
        await expect(dialog).to_have_count(0)
        body = submitted[-1][1]
        auth = body["auth"] if kind == "personal" else body["config"]["auth"]
        assert auth["auth_mode"] == "prefer_cimd_fallback_dcr" and auth["token_endpoint_auth_method"] == "client_secret_basic"
        assert auth["callback_path"] == "/callback"
        assert "client_id" not in auth and "client_secret" not in auth
    assert len(submitted) == 4


async def test_default_and_legacy_modes_share_four_options_and_ignore_inactive_fields(runtime_browser):
    page, _ = runtime_browser
    for initial, expected in [
        ({}, "prefer_cimd_fallback_dcr"),
        ({"auth_mode": "manual", "client_id": "manual"}, "pre_registration"),
        ({"auth_mode": "auto", "allow_dcr": False}, "cimd_only"),
        ({"auth_mode": "auto", "allow_dcr": True}, "prefer_cimd_fallback_dcr"),
        ({"auth_mode": "dcr"}, "dcr_only"),
    ]:
        await page.evaluate("""async(initial)=>{
          const {oauthFields}=await import('/oauth-fields.js');
          window.oauthForm=oauthFields(initial);
          document.querySelector('main').replaceChildren(oauthForm.node);
        }""", initial)
        mode = page.get_by_label("OAuth 客户端注册方式", exact=True)
        await expect(mode).to_have_value(expected)
        await expect(mode.locator("option")).to_have_count(4)
        await expect(page.get_by_role("checkbox")).to_have_count(0)
        value = await page.evaluate("oauthForm.value()")
        assert value["auth_mode"] == expected and "allow_dcr" not in value
        await mode.select_option("cimd_only")
        await expect(page.get_by_label("客户端名称（client_name）", exact=True)).not_to_be_visible()
        value = await page.evaluate("oauthForm.value()")
        assert not {"client_id", "client_secret", "client_name", "allow_dcr"} & value.keys()
        await mode.select_option("dcr_only")
        await page.get_by_label("客户端名称（client_name）", exact=True).fill("Custom DCR")
        await mode.select_option("prefer_cimd_fallback_dcr")
        assert (await page.evaluate("oauthForm.value()"))["client_name"] == "Custom DCR"
        await expect(page.get_by_text("DCR：动态客户端注册，MMG 直接向服务器注册并获取 Client ID/Secret，不需要公网地址。", exact=True)).to_be_visible()
