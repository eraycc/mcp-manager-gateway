import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_TRANSLATION_CONFIG,
  applyTranslation,
  changeTranslationLanguage,
  clearTranslationCache,
  disableTranslation,
  normalizeTranslationConfig,
  parseList,
  parseTerminology,
} from '../../src/mcp_manager/static/translation.js';

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
});

test('translation applies explicit target without duplicate or undefined ignore rules',async()=>{
  const pushed={class:[],id:[],tag:[],text:[]},languages=[];
  const ignore=Object.fromEntries(Object.keys(pushed).map(key=>[key,{
    push(value){
      assert.notEqual(value,undefined);
      assert.ok(!pushed[key].includes(value),'duplicate '+key+' ignore');
      pushed[key].push(value);
    },
  }]));
  globalThis.translate={
    ignore,
    language:{setLocal(value){languages.push(['local',value])}},
    service:{use(){}},
    listener:{start(){},stop(){}},
    whole:{enableAll(){}},
    execute(){languages.push(['execute'])},
    changeLanguage(value){languages.push(['change',value])},
  };
  const config={...DEFAULT_TRANSLATION_CONFIG,enabled:true,ignore:{class:[],id:[],tag:['code','pre'],text:[]}};
  await applyTranslation(config,'japanese');
  await applyTranslation(config,'japanese');
  await changeTranslationLanguage(config,'chinese_simplified');
  assert.deepEqual(pushed.tag,['code','pre']);
  assert.ok(languages.some(call=>call[0]==='change'&&call[1]==='japanese'));
  assert.ok(languages.some(call=>call[0]==='change'&&call[1]==='chinese_simplified'));
  delete globalThis.translate;
});

test('translation cache cleanup counts hash entries and disable restores local language',async()=>{
  const values=new Map([
    ['hash_english_1','a'],['hash_english_2','b'],['hash_japanese_3','c'],
    ['mcp-translation-target','english'],['other','keep'],
  ]);
  globalThis.localStorage={
    get length(){return values.size},
    key(index){return [...values.keys()][index]??null},
    removeItem(key){values.delete(key)},
  };
  const calls=[];
  globalThis.translate={
    language:{clearCacheLanguage(){calls.push('clear')}},
    listener:{stop(){calls.push('stop')}},
    changeLanguage(value){calls.push(value)},
  };
  assert.equal(await clearTranslationCache(),3);
  assert.deepEqual([...values.keys()].sort(),['mcp-translation-target','other']);
  await disableTranslation(DEFAULT_TRANSLATION_CONFIG);
  assert.deepEqual(calls,['clear','stop','chinese_simplified']);
  delete globalThis.translate;
  delete globalThis.localStorage;
});
