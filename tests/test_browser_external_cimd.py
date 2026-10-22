"""Shared CIMD editor at desktop and narrow mobile widths."""
import asyncio

import pytest
import test_browser_runtime_requirements as browser_fixtures
from playwright.async_api import expect

runtime_browser = browser_fixtures.runtime_browser
local_update_check = browser_fixtures.local_update_check
URL = "https://metadata.example.com/" + "a" * 100 + "/mmg.json"


@pytest.mark.parametrize("width", [320, 390, 1280])
async def test_external_editor_generate_edit_verify_and_stale_draft(runtime_browser, width):
    page, url = runtime_browser
    await page.set_viewport_size({"width": width, "height": 900})
    await page.evaluate("""async()=>{
      const {oauthFields}=await import('/oauth-fields.js');
      const {dialog}=await import('/core.js');
      window.oauthForm=oauthFields({type:'oauth',auth_mode:'cimd_only'});
      dialog('CIMD test',oauthForm.node);
    }""")
    hosting = page.get_by_label("CIMD 元数据托管方式", exact=True)
    await expect(hosting).to_have_value("mmg")
    await hosting.select_option("external")
    address = page.get_by_label("客户端元数据托管 URL", exact=True)
    await address.fill(URL)
    await expect(page.get_by_text("手机上的 127.0.0.1 指向手机", exact=False)).to_be_visible()
    await page.get_by_role("button", name="生成 JSON", exact=True).click()
    editor = page.get_by_label("元数据 JSON", exact=True)
    await expect(editor).to_be_visible()
    text = await editor.input_value()
    import json
    value = json.loads(text)
    assert value["client_id"] == URL
    assert value["redirect_uris"] == [url + "/api/v1/oauth/callback"]
    await hosting.scroll_into_view_if_needed()
    await page.screenshot(path=f"artifacts/qa/cimd/{width}.png")
    async def verified(route):
        body = route.request.post_data_json
        assert body["url"] == URL and body["metadata"]["client_name"] == "Private MMG"
        await route.fulfill(json={"ok": True, "status": "verified", "verified_at": 1792600000,
                                  "code": None, "message": "已验证线上元数据"})
    await page.route("**/api/v1/oauth/client-metadata/verify", verified)
    value["client_name"] = "Private MMG"
    await editor.fill(json.dumps(value))
    await page.get_by_role("button", name="验证线上文件", exact=True).click()
    await expect(page.locator(".cimd-status")).to_contain_text("已验证")
    saved = await page.evaluate("oauthForm.value()")
    assert saved["cimd_metadata_source"] == "external" and saved["cimd_external_url"] == URL
    assert saved["cimd_metadata"]["client_name"] == "Private MMG"
    assert not any(key.startswith("cimd_verified") for key in saved)
    await address.fill(URL + "?changed=1")
    await expect(page.locator(".cimd-status")).to_contain_text("已过期")
    await hosting.select_option("mmg")
    await hosting.select_option("external")
    await expect(address).to_have_value(URL + "?changed=1")
    await expect(editor).to_have_value(json.dumps(value))
    mode = page.get_by_label("OAuth 客户端注册方式", exact=True)
    await mode.select_option("dcr_only")
    await expect(hosting).not_to_be_visible()
    await mode.select_option("cimd_only")
    await expect(address).to_have_value(URL + "?changed=1")
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert await page.get_by_role("dialog").evaluate("(n)=>n.scrollWidth<=n.clientWidth+1")
    for button in await page.locator(".cimd-fields button").all():
        if await button.is_visible():
            assert (await button.bounding_box())["height"] >= 44


async def test_verify_result_for_old_url_cannot_mark_new_url_verified(runtime_browser):
    page, _ = runtime_browser
    await page.evaluate("""async()=>{
      const {oauthFields}=await import('/oauth-fields.js');
      window.oauthForm=oauthFields({type:'oauth',auth_mode:'cimd_only',
        cimd_metadata_source:'external',cimd_external_url:'https://metadata.example.com/old.json'});
      document.querySelector('main').replaceChildren(oauthForm.node);
    }""")
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(route):
        entered.set()
        await release.wait()
        await route.fulfill(json={"ok":True,"status":"verified","verified_at":1792600000})
    await page.route("**/api/v1/oauth/client-metadata/verify", delayed)
    await page.get_by_role("button", name="验证线上文件", exact=True).click()
    await asyncio.wait_for(entered.wait(), 5)
    await page.get_by_label("客户端元数据托管 URL", exact=True).fill("https://metadata.example.com/new.json")
    release.set()
    await expect(page.get_by_role("button", name="验证线上文件", exact=True)).to_be_enabled()
    await expect(page.locator(".cimd-status")).not_to_contain_text("已验证")


async def test_public_export_rejects_secrets_before_download(runtime_browser):
    page, _ = runtime_browser
    await page.evaluate("""async()=>{
      const {oauthFields}=await import('/oauth-fields.js');
      window.oauthForm=oauthFields({type:'oauth',auth_mode:'cimd_only',
        cimd_metadata_source:'external',cimd_external_url:'https://metadata.example.com/mmg.json',
        cimd_metadata:{client_id:'https://metadata.example.com/mmg.json',client_name:'MMG',
          redirect_uris:['http://127.0.0.1:8765/callback'],client_secret:'do-not-publish'}});
      document.querySelector('main').replaceChildren(oauthForm.node);
    }""")
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    await page.get_by_role("button", name="下载 JSON", exact=True).click()
    await expect(page.locator(".cimd-status")).to_contain_text("Secret")
    assert not downloads


async def test_copy_and_download_export_the_current_json(runtime_browser):
    import json

    page, _ = runtime_browser
    await page.evaluate("""async()=>{
      const {oauthFields}=await import('/oauth-fields.js');
      window.oauthForm=oauthFields({type:'oauth',auth_mode:'cimd_only',
        cimd_metadata_source:'external',cimd_external_url:'https://metadata.example.com/mmg.json'});
      document.querySelector('main').replaceChildren(oauthForm.node);
      Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copied=text}},configurable:true});
      const original=URL.createObjectURL;
      URL.createObjectURL=blob=>{window.exported=blob;return original(blob)};
    }""")
    await page.get_by_role("button", name="生成 JSON", exact=True).click()
    editor = page.get_by_label("元数据 JSON", exact=True)
    await expect(editor).to_be_visible()
    expected = json.loads(await editor.input_value())
    await page.get_by_role("button", name="复制 JSON", exact=True).click()
    assert json.loads(await page.evaluate("copied")) == expected
    async with page.expect_download() as pending:
        await page.get_by_role("button", name="下载 JSON", exact=True).click()
    assert (await pending.value).suggested_filename == "client-metadata.json"
    assert json.loads(await page.evaluate("exported.text()")) == expected
