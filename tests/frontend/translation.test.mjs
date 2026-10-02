import test from 'node:test';
import assert from 'node:assert/strict';
import {DEFAULT_TRANSLATION_CONFIG,normalizeTranslationConfig,parseList,parseTerminology} from '../../src/mcp_manager/static/translation.js';

test('translation config normalizes lists, service and advanced options',()=>{
  const value=normalizeTranslationConfig({
    enabled:true,
    local_language:'chinese_simplified',
    target_language:'english',
    service:'custom',
    custom_host:'https://translate.example.com/',
    sse_enabled:true,
    ignore:{class:[' notranslate ','notranslate'],id:'menu\nheader',tag:['code'],text:['MCP']},
    terminology:[{source:'网关',target:'gateway'},{source:'',target:'drop'}],
    url_control:true,
    url_parameter:'lang',
    dynamic_content:true,
    whole_page:false,
    translate_local:false,
    queue_enabled:true,
  });
  assert.deepEqual(value.ignore.class,['notranslate']);
  assert.deepEqual(value.ignore.id,['menu','header']);
  assert.deepEqual(value.terminology,[{source:'网关',target:'gateway'}]);
  assert.equal(value.custom_host,'https://translate.example.com');
  assert.equal(value.sse_enabled,true);
});

test('translation text helpers support import and form editing',()=>{
  assert.deepEqual(parseList('a\nb, c\na'),['a','b','c']);
  assert.deepEqual(parseTerminology('MCP = MCP\n网关 = gateway\ninvalid'),[
    {source:'MCP',target:'MCP'},
    {source:'网关',target:'gateway'},
  ]);
  assert.equal(DEFAULT_TRANSLATION_CONFIG.service,'client.edge');
  assert.equal(DEFAULT_TRANSLATION_CONFIG.local_language,'chinese_simplified');
  assert.equal(DEFAULT_TRANSLATION_CONFIG.target_language,'english');
});
