import test from 'node:test';
import assert from 'node:assert/strict';
import {abortableDelay,api,allPages,errorText,jobProgressModel,previewJson,watchJob} from '../../src/mcp_manager/static/core.js';
import {CAPABILITY_PAGE_SIZE,capabilityDetailUrl,capabilityListUrl} from '../../src/mcp_manager/static/capability-browser.js';
import {importPage} from '../../src/mcp_manager/static/importer.js';

test('mutations send cookie CSRF token and JSON with same-origin credentials', async()=>{
  globalThis.document={cookie:'other=x; mcp_csrf=abc%2Fdef'};
  globalThis.fetch=async(url,options)=>{
    assert.equal(url,'/api/v1/tokens');
    assert.equal(options.credentials,'include');
    assert.equal(options.headers['X-CSRF-Token'],'abc/def');
    assert.deepEqual(JSON.parse(options.body),{name:'测试'});
    return new Response(JSON.stringify({id:'one'}),{status:200});
  };
  assert.deepEqual(await api('/tokens',{method:'POST',body:{name:'测试'}}),{id:'one'});
});

test('validation errors remain legible and preserve HTTP status',async()=>{
  globalThis.document={cookie:''};
  globalThis.fetch=async()=>new Response(JSON.stringify({detail:[{loc:['body','name'],msg:'必填'}]}),{status:422});
  await assert.rejects(api('/mcps',{method:'POST',body:{}}),e=>e.status===422&&e.message==='body.name 必填');
  assert.equal(errorText({detail:'权限不足'}),'权限不足');
});

test('all matching selection traverses pages while retaining filters',async()=>{
  globalThis.document={cookie:''};
  const seen=[],progress=[];
  globalThis.fetch=async(url)=>{
    const u=new URL(url,'http://localhost');seen.push(u);
    const page=Number(u.searchParams.get('page'));
    return new Response(JSON.stringify({items:[{id:page}],total:2,total_pages:2}));
  };
  const items=await allPages('/mcps',{q:'服务',transport:'stdio',page:9},(n,total)=>progress.push([n,total]));
  assert.deepEqual(items,[{id:1},{id:2}]);
  assert.equal(seen[1].searchParams.get('q'),'服务');
  assert.equal(seen[1].searchParams.get('transport'),'stdio');
  assert.equal(seen[1].searchParams.get('page_size'),'100');
  assert.deepEqual(progress,[[1,2],[2,2]]);
});

test('completed-with-errors job is terminal and must not poll forever',async()=>{
  globalThis.fetch=async()=>{throw new Error('must not poll')};
  const job={id:'job',status:'completed_with_errors',results:[{ok:false,error:'tool failed'}]};
  assert.equal(await watchJob(job),job);
});

test('abort signal reaches fetch without being swallowed',async()=>{
  globalThis.document={cookie:''};
  const controller=new AbortController();controller.abort();
  globalThis.fetch=async(_url,options)=>{assert.equal(options.signal,controller.signal);options.signal.throwIfAborted()};
  await assert.rejects(api('/dashboard',{signal:controller.signal}),e=>e.name==='AbortError');
});

test('interrupted restored jobs are terminal',async()=>{globalThis.fetch=async()=>{throw new Error('must not poll')};const job={id:'restored',status:'interrupted'};assert.equal(await watchJob(job),job)});

test('large capability views stay on a fifty-item summary page and encode exact details',()=>{
  assert.equal(CAPABILITY_PAGE_SIZE,50);
  const url=new URL(capabilityListUrl('/mcps/service/capabilities',{kind:'tools',q:'中文 / tool',cursor:'next',limit:1000}),'http://localhost');
  assert.equal(url.searchParams.get('limit'),'50');
  assert.equal(url.searchParams.get('q'),'中文 / tool');
  assert.equal(url.searchParams.get('cursor'),'next');
  assert.equal(capabilityDetailUrl('/mcps/service/capabilities','tools','folder/name'),'/mcps/service/capabilities/tools/folder%2Fname');
});

test('capability definitions use exact identifiers instead of display labels',()=>{
  const root='/mcps/service/capabilities';
  assert.equal(capabilityDetailUrl(root,'resources',{name:'Health',uri:'device://health'}),
    root+'/resources/device%3A%2F%2Fhealth');
  assert.equal(capabilityDetailUrl(root,'templates',{name:'Device',uriTemplate:'device://{key}'}),
    root+'/templates/device%3A%2F%2F%7Bkey%7D');
  assert.equal(capabilityDetailUrl(root,'prompts',{name:'status',gateway_name:'device__status'}),
    root+'/prompts/status');
  assert.equal(capabilityDetailUrl(root,'tools',{name:'echo',gateway_name:'device__echo'}),
    root+'/tools/echo');
});

test('job progress model contains counters only and clamps invalid progress',()=>{
  assert.deepEqual(jobProgressModel({status:'running',total:10,completed:12,succeeded:8,failed:2,results:['large']}),{
    total:10,completed:10,succeeded:8,failed:2,cancelled:0,percent:100,status:'running',terminal:false,error:''
  });
});

test('automatic JSON previews remain within their byte budget',()=>{
  const preview=previewJson({text:'中'.repeat(100_000)},65536);
  assert.equal(preview.truncated,true);
  assert.ok(new TextEncoder().encode(preview.text).length<=65536);
});

test('ten thousand imports only expose the current fifty-row window',()=>{
  const entries=Array.from({length:10_000},(_,index)=>({item:{name:'service-'+String(index).padStart(5,'0')},status:'未诊断'}));
  const first=importPage(entries,'',1);
  assert.equal(first.items.length,50);
  assert.equal(first.total,10_000);
  assert.equal(first.pages,200);
  const filtered=importPage(entries,'service-09999',1);
  assert.equal(filtered.items.length,1);
});

test('abortable delay removes its listener after an ordinary tick',async()=>{
  const listeners=new Set();
  const signal={aborted:false,addEventListener(_name,fn){listeners.add(fn)},removeEventListener(_name,fn){listeners.delete(fn)}};
  await abortableDelay(0,signal);
  assert.equal(listeners.size,0);
});
