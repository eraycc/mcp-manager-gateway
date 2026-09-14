import{el,api,button,field,check}from './core.js';

export async function embeddingSettings(signal){
 const saved=await api('/settings/embedding',{signal});
 const card=el('section',{class:'card stack'},el('h2',{},'工具检索'),
  el('p',{class:'notice'},'按需发现优先匹配精确名称，并结合字段关键词、模糊匹配与向量语义检索。向量接口不可用时自动回退关键词检索。'));
 const enabled=check('启用向量语义检索',saved.enabled),grid=el('div',{class:'form-grid settings-grid'});
 const definitions=[
  ['base_url','Embedding 接口地址','url','例如 http://localhost:8098 或 http://localhost:8098/v1；也支持完整 /embeddings 地址。'],
  ['model','Embedding 模型','text','填写接口实际提供的 embedding 模型名，例如 BAAI/bge-m3。reranker 模型不能用于此接口。'],
  ['api_key','Embedding API Key','password','密钥加密保存。[REDACTED] 表示保留现有密钥，清空后保存则删除密钥。'],
  ['timeout_seconds','Embedding 超时（秒）','number','接口不可用或超时后回退关键词检索。'],
  ['min_similarity','最低语义相似度','number','范围 -1 到 1，默认 0.5；阈值越高，语义候选越少。已保存的自定义值不会自动更改。']
 ];
 const fields={};
 for(const[key,label,type,hint]of definitions){
  const f=field(label,saved[key],type,hint);f.input.setAttribute('aria-label',label);fields[key]=f.input;
  if(key==='timeout_seconds'){f.input.min=.1;f.input.max=60;f.input.step=.1}
  if(key==='min_similarity'){f.input.min=-1;f.input.max=1;f.input.step=.01}
  grid.append(f.node);
 }
 const status=el('p',{role:'status','aria-live':'polite'}),errors=el('p',{class:'error',role:'alert',hidden:true});
 const payload=()=>({enabled:enabled.input.checked,...Object.fromEntries(definitions.map(([key,,type])=>
  [key,type==='number'?Number(fields[key].value):fields[key].value]))});
 const busy=value=>{save.disabled=value;probe.disabled=value};
 const save=button('保存检索设置',async()=>{
  busy(true);errors.hidden=true;status.textContent='正在保存…';
  try{
   const result=await api('/settings/embedding',{method:'PATCH',body:payload()});
   for(const[key]of definitions)fields[key].value=result[key];
   status.textContent='检索设置已保存。';
  }catch(e){status.textContent='';errors.textContent=e.message;errors.hidden=false}
  finally{busy(false)}
 },'primary');
 const probe=button('测试已保存的连接',async()=>{
  busy(true);errors.hidden=true;status.textContent='正在测试已保存的模型配置…';
  try{
   const result=await api('/settings/embedding/test',{method:'POST'});
   status.textContent=result.status==='ready'?'连接正常：'+result.model+'，向量维度 '+result.dimension+'。':
    result.status==='disabled'?'向量检索未启用，请启用并保存后测试。':'连接不可用，检索将回退关键词模式。原因：'+(result.reason||'未知');
  }catch(e){status.textContent='';errors.textContent=e.message;errors.hidden=false}
  finally{busy(false)}
 });
 card.append(enabled.node,grid,el('p',{class:'notice'},'仅向该接口发送服务与工具的描述性元数据和检索语句，不发送 MCP 连接凭据或实际工具调用参数。'),
  el('div',{class:'actions'},save,probe),status,errors);
 return card;
}
