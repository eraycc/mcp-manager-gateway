import test from 'node:test';
import assert from 'node:assert/strict';
import {api,allPages,errorText,watchJob} from '../../src/mcp_manager/static/core.js';

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
