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
    await expect(mode).to_have_value("manual")
    await expect(identity).to_have_attribute("required", "")
    assert not await secret.evaluate("(n)=>n.required")
    await mode.select_option("auto")
    await expect(identity).not_to_be_visible()
    await expect(identity).to_have_value("")
    await expect(secret).to_have_value("")
    await page.get_by_label("允许 DCR 回退（仅服务器不支持 CIMD 时）", exact=True).check()
    await expect(name).to_be_visible()
    await expect(name).to_have_value("Codex")
    automatic = await page.evaluate("oauthForm.value()")
    assert automatic["allow_dcr"] and automatic["client_name"] == "Codex"
    assert "client_id" not in automatic and "client_secret" not in automatic
    await name.fill("My MMG")
    await mode.select_option("manual")
    await expect(name).not_to_be_visible()
    await expect(name).to_have_value("")
    await identity.fill("public-client")
    manual = await page.evaluate("oauthForm.value()")
    assert manual["client_id"] == "public-client"
    assert "client_secret" not in manual and "client_name" not in manual and "allow_dcr" not in manual
