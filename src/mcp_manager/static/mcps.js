import{oauthFields}from './oauth-fields.js';
import{el,api,button,toast,errorText,field,selectField,check,jsonField,dialog,formDialog,detail,pretty,jobDialog,loading,empty,badge,renderFullJson}from './core.js';
import{capabilityBrowser}from './capability-browser.js';
export {mcpImport} from './importer.js';
const transportOptions=[['stdio','本地进程 · stdio'],['streamable-http','Streamable HTTP'],['sse','Server-Sent Events'],['rest','REST 转 MCP']];
export const isolationOptions=transport=>transport==='stdio'?[['service','服务共享'],['user','按用户隔离'],['session','按会话隔离']]:[['service','服务共享'],['user','按用户隔离']];
export const normalizeIsolation=(transport,isolation)=>transport!=='stdio'&&isolation==='session'?'user':isolation;
const restExample={name:'get_item',description:'查询条目',inputSchema:{type:'object',properties:{id:{type:'string',description:'条目 ID'}},required:['id']},request:{method:'GET',url:'https://api.example.com/items/{id}',headers:{},query:{}},response:{type:'json',pointer:'/data',mapping:{}}};
const editorSecretKeys=new Set(['authorization','password','current_password','secret','secret_key','token','access_token','refresh_token','client_secret','registration_access_token','id_token','code_verifier','cookie','database_url','api_key','value']);
export function maskEditorSecrets(value,parent=''){if(Array.isArray(value))return value.map(item=>maskEditorSecrets(item,parent));if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([key,item])=>[key,(editorSecretKeys.has(key.toLowerCase())||['env','headers','env_headers'].includes(parent))&&item?'[REDACTED]':maskEditorSecrets(item,key.toLowerCase())]));return value}
function mergeEditorSecrets(current,revealed){if(current==='[REDACTED]')return structuredClone(revealed);if(Array.isArray(current))return current.map((item,index)=>mergeEditorSecrets(item,revealed?.[index]));if(current&&typeof current==='object')return Object.fromEntries(Object.entries(current).map(([key,item])=>[key,mergeEditorSecrets(item,revealed?.[key])]));return current}
function proposalEditorModel(full){const payload=structuredClone(full.payload);payload.slug=payload.slug||'';if(payload.config?.auth?.type==='oauth'&&full.config_isolation)payload.config.auth.config_isolation=full.config_isolation;return{...payload,mode:full.mode||'lazy',isolation:full.isolation||'service',config_isolation:full.config_isolation??null}}
export async function mcpEditor(row,refresh=()=>{},options={}){const proposal=options.proposal===true,resource=proposal?'/mcp-proposals/'+row.id:row?'/mcps/'+row.id:'/mcps',load=async reveal=>{const data=await api(resource+(reveal?'?reveal=true':''));return proposal?proposalEditorModel(data):data};let model=row?await load(false):{name:'',slug:'',description:'',tags:[],transport:'stdio',mode:'lazy',isolation:'service',config:{command:'',args:[],env:{},startup_timeout:30,call_timeout:60,stop_timeout:10,queue_timeout:30,concurrency:1,idle_seconds:86400}};model=structuredClone(model);let mode='form',readForm=()=>model;const drafts=new Map();let secretsVisible=false,secretDraft=null;
const body=el('div',{class:'stack'}),formArea=el('div',{class:'stack'}),json=jsonField('完整配置 JSON',model,'表单与 JSON 共用同一份配置；未在表单中显示的扩展字段会保留。');json.node.hidden=true;const err=el('div',{class:'error',hidden:true,role:'alert'});
function build(){formArea.replaceChildren();const name=field('服务名称',model.name),slug=field('服务标识',model.slug,'text','留空由服务器生成；建议使用英文、数字与短横线。'),description=field('描述（建议填写）',model.description||'','text','用于 Agent 在一级索引判断服务用途；留空时网关会根据已发现工具生成临时摘要。'),tags=field('标签',(model.tags||[]).join(', '),'text','使用英文逗号分隔。'),transport=selectField('传输协议',model.transport,transportOptions),policy=selectField('启动策略',model.mode,[['lazy','按需启动'],['eager','常驻运行'],['disabled','禁用']]),isolation=selectField('实例隔离',normalizeIsolation(model.transport,model.isolation),isolationOptions(model.transport));name.input.required=true;slug.input.disabled=!!row&&!proposal;if(row&&!proposal)slug.node.querySelector('small').textContent='服务标识用于稳定的工具路由，创建后不可修改。';
const base=el('div',{class:'form-grid'},name.node,slug.node,description.node,tags.node,transport.node,policy.node,isolation.node),section=el('div',{class:'stack'});formArea.append(base,section);let readConfig=()=>model.config;
function configForm(){section.replaceChildren();const c=model.config||{},parts=[];let specific=()=>({});if(transport.input.value==='stdio'){const command=field('可执行命令',c.command||'','text','例如 uv、node 或可执行文件绝对路径。'),args=jsonField('命令参数',c.args||[], 'JSON 字符串数组，例如 ["run", "server.py"]'),env=jsonField('环境变量',c.env||{},'键值对象。秘密值保存后以 [REDACTED] 展示。'),cwd=field('工作目录',c.cwd||''),encoding=field('输出编码',c.encoding||'utf-8');command.input.required=true;parts.push(command.node,args.node,env.node,el('div',{class:'form-grid'},cwd.node,encoding.node));specific=()=>{const a=args.value(),e=env.value();if(!Array.isArray(a)||!a.every(x=>typeof x==='string'))throw new Error('命令参数必须是字符串数组');if(!e||Array.isArray(e)||typeof e!=='object')throw new Error('环境变量必须是对象');return{command:command.input.value,args:a,env:e,cwd:cwd.input.value||null,encoding:encoding.input.value}}}
else if(transport.input.value==='rest'){const health=field('健康检查 URL（可选）',c.healthcheck_url||'','url','填写后通过 GET 检查该地址；留空则对工具地址发送 HEAD 检查连接。含 URL 参数的工具必须填写固定健康检查地址。');parts.push(health.node);const list=el('div',{class:'stack'});let tools=structuredClone(c.tools||[]);const draw=()=>{list.replaceChildren(...tools.map((t,i)=>el('div',{class:'card'},el('div',{class:'actions'},el('strong',{},t.name||'未命名工具'),badge(t.request?.method||'GET'),el('span',{class:'muted'},t.request?.url||''),button('编辑映射',()=>restTool(t,value=>{tools[i]=value;draw()})),button('删除工具',()=>{tools.splice(i,1);draw()},'danger')))));if(!tools.length)list.append(empty('尚未配置 REST 工具，添加后可编辑请求与响应映射。'))};draw();parts.push(el('p',{class:'notice'},'每个 REST 工具独立定义输入 Schema、HTTP 请求和响应映射。使用 {id} 引用输入参数，支持 {user.id}；完整占位符保留 JSON 值类型，URL 参数自动编码。'),list,button('添加 REST 工具',()=>restTool(structuredClone(restExample),value=>{tools.push(value);draw()})));specific=()=>({tools,healthcheck_url:health.input.value||null})}
else{const url=field('服务 URL',c.url||'','url'),headers=jsonField('请求头',c.headers||{}),tls=check('验证 TLS 证书',c.verify_tls!==false);url.input.required=true;const authType=selectField('认证方式',c.auth?.type||'none',[['none','无认证'],['bearer','Bearer Token'],['basic','Basic 用户名密码'],['api_key','API Key'],['oauth','OAuth 2.0']]),authBox=el('div',{class:'form-grid'});let authFields={},oauthForm=null,authBase=structuredClone(c.auth||{});const authDrafts=new Map();
const readAuth=()=>{const a=oauthForm?oauthForm.value(false):{...authBase,type:authType.input.value};for(const[k,f]of Object.entries(authFields))a[k]=f.input.value;if(a.type==='oauth'&&!a.config_isolation)delete a.config_isolation;return a};
const drawAuth=()=>{authFields={};oauthForm=null;authBox.replaceChildren();
 if(authType.input.value==='oauth'){
  oauthForm=oauthFields(authBase,{secretsVisible});authBox.append(oauthForm.node);
  const personal=isolation.input.value==='user',choices=proposal?[['','未设置（审批后默认共享）'],['shared','共享配置'],['user','独享配置']]:[['shared','共享配置'],['user','独享配置']],
   selected=proposal?(authBase.config_isolation??''):(personal?(authBase.config_isolation||'shared'):'shared'),f=selectField('OAuth 配置隔离',selected,choices);
  f.input.disabled=!personal&&!proposal;authFields.config_isolation=f;
  authBox.append(f.node,el('p',{class:'muted'},personal?'共享配置由管理员维护；独享配置由每位用户在个人资料中填写。':'服务共享时留空或使用共享配置；独享配置仅适用于按用户隔离。'));
 }else{const defs={none:[],bearer:[['token','Bearer Token']],basic:[['username','用户名'],['password','密码']],api_key:[['header','请求头名称（例如 X-API-Key）'],['value','API Key']]};
  for(const[k,l]of defs[authType.input.value]){const f=field(l,authBase[k]||'',/password|token$|^value$/.test(k)&&!secretsVisible?'password':'text');authFields[k]=f;authBox.append(f.node)}
 }};
authType.input.onchange=()=>{
 const chosen=authType.input.value;
 authType.input.value=authBase.type||'none';
 authDrafts.set(authType.input.value,readAuth());
 authType.input.value=chosen;
 authBase=structuredClone(authDrafts.get(chosen)||{type:chosen});
 drawAuth();
};drawAuth();parts.push(url.node,headers.node,tls.node,authType.node,authBox);
specific=()=>({url:url.input.value,headers:headers.value(),verify_tls:tls.input.checked,auth:readAuth()})}
const timings={};const timingGrid=el('div',{class:'form-grid'});for(const[k,l,v]of [['startup_timeout','启动超时（秒）',30],['call_timeout','调用超时（秒）',60],['stop_timeout','停止超时（秒）',10],['queue_timeout','排队超时（秒）',30],['concurrency','并发调用数',1],['idle_seconds','空闲回收（秒）',86400]]){const f=field(l,c[k]??v,'number');f.input.min=k==='idle_seconds'?0:1;timings[k]=f;timingGrid.append(f.node)}section.append(...parts,el('details',{},el('summary',{},'超时、并发与回收设置'),timingGrid));readConfig=()=>({...c,...specific(),...Object.fromEntries(Object.entries(timings).map(([k,f])=>[k,Number(f.input.value)]))})}
readForm=()=>{const config=readConfig();return{...model,name:name.input.value,slug:slug.input.value||undefined,description:description.input.value,tags:tags.input.value.split(',').map(x=>x.trim()).filter(Boolean),transport:transport.input.value,mode:policy.input.value,isolation:isolation.input.value,config_isolation:config.auth?.type==='oauth'?(config.auth.config_isolation||null):null,config}};const redrawIsolation=(chosen,value)=>{const normalized=normalizeIsolation(chosen,value);isolation.input.replaceChildren(...isolationOptions(chosen).map(([v,l])=>el('option',{value:v},l)));isolation.input.value=normalized;return normalized};isolation.input.onchange=()=>{model=readForm();configForm()};let previousTransport=model.transport;transport.input.onchange=()=>{const chosen=transport.input.value;try{transport.input.value=previousTransport;const saved=readForm(),before=saved.isolation;drafts.set(previousTransport,structuredClone(saved.config));model={...saved,transport:chosen,config:drafts.has(chosen)?structuredClone(drafts.get(chosen)):{...transportTemplate(chosen),...saved.config}};transport.input.value=chosen;model.isolation=redrawIsolation(chosen,before);previousTransport=chosen;configForm();if(model.isolation!==before)toast('网络传输不支持会话隔离，已切换为按用户隔离','warning');err.hidden=true}catch(e){transport.input.value=previousTransport;err.textContent='协议未切换：'+e.message;err.hidden=false}};configForm()}
build();const formBtn=button('表单配置',()=>switchMode('form'),'active'),jsonBtn=button('JSON 配置',()=>switchMode('json'));function switchMode(next){try{if(next===mode)return;if(mode==='form'){model=readForm();json.input.value=pretty(model)}else{model=json.value();if(!model||!transportOptions.some(x=>x[0]===model.transport)||!model.config)throw new Error('配置需要合法 transport 与 config 对象');build()}mode=next;formArea.hidden=mode!=='form';json.node.hidden=mode!=='json';formBtn.classList.toggle('active',mode==='form');jsonBtn.classList.toggle('active',mode==='json');err.hidden=true}catch(e){err.textContent=e.message;err.hidden=false}}
const current=()=>mode==='form'?readForm():json.value();
const effective=currentValue=>secretDraft?mergeEditorSecrets(currentValue,secretDraft):currentValue;
const templateButton=button('补全当前协议模板',()=>{try{const c=current();model={...c,config:{...transportTemplate(c.transport),...c.config}};if(mode==='json')json.input.value=pretty(model);else build();err.hidden=true;toast('已补全缺省字段，现有配置与扩展字段已保留')}catch(e){err.textContent=e.message;err.hidden=false}});
const secretButton=button('显示敏感字段',async()=>{secretButton.disabled=true;err.hidden=true;try{const c=current();if(!secretsVisible){const revealed=secretDraft||await load(true);model=mergeEditorSecrets(c,revealed);secretsVisible=true;secretButton.textContent='隐藏敏感字段'}else{secretDraft=structuredClone(c);model=maskEditorSecrets(c);secretsVisible=false;secretButton.textContent='显示敏感字段'}if(mode==='json')json.input.value=pretty(model);else build()}catch(e){err.textContent=e.message;err.hidden=false}finally{secretButton.disabled=!row}});
secretButton.disabled=!row;
const proposalPayload=c=>{const payload=Object.fromEntries(['name','slug','description','tags','transport','mode','isolation','config'].filter(k=>c[k]!==undefined).map(k=>[k,c[k]]));payload.config_isolation=Object.hasOwn(c,'config_isolation')?c.config_isolation:(c.config?.auth?.config_isolation??null);return payload};
const persistProposal=c=>api(resource,{method:'PATCH',body:proposalPayload(effective(c))});
const testButton=button(proposal?'保存并测试':'诊断连接',async()=>{err.hidden=true;try{const c=effective(current());if(proposal){await persistProposal(c);if(c.config?.auth?.type==='oauth'){toast('OAuth 类型需要在审批通过并授权后验证','warning');return}detail('测试结果',await api(resource+'/test?summary=true',{method:'POST'}));toast('测试成功','success');refresh()}else detail('诊断结果',await api('/mcps/diagnose',{method:'POST',body:{server_id:row?.id,revision:row?.revision,transport:c.transport,config:c.config,mode:c.mode,isolation:c.isolation,summary:true}}))}catch(e){err.textContent=e.message;err.hidden=false}});
const submit=el('button',{type:'submit',class:'primary'},proposal?'保存审批配置':row?'保存修改':'创建服务');
const form=el('form',{class:'stack'},el('div',{class:'tabs'},formBtn,jsonBtn),formArea,json.node,el('div',{class:'actions'},templateButton,secretButton),err,el('div',{class:'actions dialog-foot'},testButton,submit));
form.noValidate=true;body.append(form);
const d=dialog(proposal?'配置 MCP 提议 · '+row.name:row?'编辑服务 · '+row.name:'新增 MCP 服务',body);
form.onsubmit=async e=>{e.preventDefault();submit.disabled=true;err.hidden=true;try{const c=effective(current());if(!c.name?.trim())throw new Error('请输入服务名称');if(proposal){await persistProposal(c);d.close();toast('审批配置已保存','success');refresh();return}const payload=Object.fromEntries(['name','slug','description','tags','transport','mode','isolation','config','revision'].filter(k=>c[k]!==undefined).map(k=>[k,c[k]]));const saved=await api(resource,{method:row?'PATCH':'POST',body:payload});d.close();if(saved.cache_status==='error'||saved.cache_status==='auth_required')toast('配置已保存，工具目录尚未就绪。'+errorText({detail:saved.cache_error||saved.last_startup_error,code:saved.cache_error_code||saved.last_startup_error_code}),'warning');else toast('服务配置已保存','success');refresh()}catch(x){err.textContent=x.message;err.hidden=false}finally{submit.disabled=false}}}
export const proposalEditor=(row,refresh)=>mcpEditor(row,refresh,{proposal:true});
function restTool(value,save){let model=structuredClone(value);const name=field('工具名称',model.name),description=field('工具描述',model.description||''),schema=jsonField('输入 JSON Schema',model.inputSchema||{type:'object',properties:{}}),method=selectField('HTTP 方法',model.request?.method||'GET',['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS']),url=field('请求 URL',model.request?.url||''),headers=jsonField('请求头',model.request?.headers||{}),query=jsonField('查询参数',model.request?.query||{}),request=jsonField('完整请求配置',model.request||{},'支持 method、url、headers、query、json 或 body。上方同名字段优先；其他扩展字段保留。'),response=jsonField('响应映射',model.response||{type:'json',pointer:'',mapping:{}},'示例：{"type":"json","pointer":"/data","mapping":{"name":"/name"}}。类型支持 json、text、image、audio。');name.input.required=true;url.input.required=true;formDialog('REST 工具与响应映射',[el('div',{class:'form-grid'},name.node,description.node,method.node,url.node),schema.node,headers.node,query.node,request.node,response.node],async()=>{const s=schema.value(),resp=response.value();if(!['json','text','image','audio'].includes(resp.type))throw new Error('响应类型必须为 json、text、image 或 audio');save({...model,name:name.input.value,description:description.input.value,inputSchema:s,request:{...request.value(),method:method.input.value,url:url.input.value,headers:headers.value(),query:query.value()},response:resp})})}
export function mcpTools(service,canManage){return capabilityBrowser({resource:'/mcps/'+service.id+'/capabilities',title:service.name+' · 能力目录',onTool:canManage?tool=>testTool(service,tool):null,refreshAction:canManage?async()=>{await api('/mcps/'+service.id+'/refresh',{method:'POST'});toast('缓存已刷新','success')}:null,actions:canManage?[button('测试用例',()=>testCases(service))]:[]})}
async function testCases(service){const panel=el('div',{class:'stack'},loading('正在加载测试用例…')),popup=dialog(service.name+' · 测试用例',panel);try{const data=await api('/mcps/'+service.id+'/test-cases');if(!popup.isConnected)return;const fieldset=jsonField('显式测试用例',data.cases||[],'仅运行你配置并启用的用例。格式：[{"id":"case-1","name":"查询测试","tool":"工具名","arguments":{},"enabled":true}]');panel.replaceChildren(el('p',{class:'notice'},'批量测试只运行保存的显式用例，不会自动调用未知工具。'),fieldset.node,el('div',{class:'actions'},button('保存用例',async()=>{const cases=fieldset.value();if(!Array.isArray(cases))throw new Error('用例必须为数组');await api('/mcps/'+service.id+'/test-cases',{method:'PUT',body:{cases}});toast('用例已保存')}),button('运行已保存用例',async()=>jobDialog(await api('/mcps/'+service.id+'/test-all',{method:'POST'})),'primary')))}catch(error){if(popup.isConnected)panel.replaceChildren(el('div',{class:'error',role:'alert'},error.message))}}
function testTool(service,tool){const abort=new AbortController();const schema=tool.inputSchema||{},props=schema.properties||{},fields={},generated=el('div',{class:'form-grid'}),args=jsonField('工具参数 JSON',{},'复杂对象与数组可直接使用 JSON 编辑。');let jsonMode=false;for(const[k,p]of Object.entries(props)){let f;if(p.enum)f=selectField(k,p.default??p.enum[0],p.enum.map(x=>[String(x),String(x)]));else if(p.type==='boolean')f=check(k,p.default??false);else if(['object','array'].includes(p.type))f=jsonField(k,p.default??(p.type==='array'?[]:{}));else f=field(k,p.default??'',p.type==='number'||p.type==='integer'?'number':'text',p.description||'');if(schema.required?.includes(k)&&p.type!=='boolean')f.input.required=true;fields[k]=[p,f];generated.append(f.node)}const read=()=>{const out={};for(const[k,[p,f]]of Object.entries(fields)){if(p.type==='boolean')out[k]=f.input.checked;else if(f.input.value!==''||schema.required?.includes(k)){out[k]=p.enum?p.enum.find(v=>String(v)===f.input.value):['object','array'].includes(p.type)?f.value():['number','integer'].includes(p.type)?Number(f.input.value):f.input.value}}return out};args.node.hidden=true;const toggle=button('切换 JSON 参数',()=>{if(!jsonMode){args.input.value=pretty(read());jsonMode=true;generated.hidden=true;args.node.hidden=false;toggle.textContent='使用表单参数'}else{const parsed=args.value();for(const[k,[p,f]]of Object.entries(fields)){if(p.type==='boolean')f.input.checked=!!parsed[k];else f.input.value=['object','array'].includes(p.type)?pretty(parsed[k]??(p.type==='array'?[]:{})):parsed[k]??''}const extra=Object.keys(parsed).filter(k=>!fields[k]);if(extra.length)throw new Error('JSON 含扩展参数 '+extra.join(', ')+'，请保持 JSON 模式以保留它们');jsonMode=false;generated.hidden=false;args.node.hidden=true;toggle.textContent='切换 JSON 参数'}});if(!Object.keys(props).length){jsonMode=true;generated.hidden=true;args.node.hidden=false}const result=el('pre',{},'执行结果将在这里显示。'),err=el('div',{class:'error',hidden:true}),submit=el('button',{type:'submit',class:'primary'},'执行工具');const form=el('form',{class:'stack'},el('p',{class:'notice'},'此操作将真实调用下游工具，可能产生数据变更。请检查参数后执行。'),toggle,generated,args.node,err,submit,result);form.noValidate=true;form.onsubmit=async e=>{e.preventDefault();submit.disabled=true;err.hidden=true;result.textContent='正在执行…';try{const value=await api('/mcps/'+service.id+'/test',{method:'POST',body:{tool:tool.name,arguments:jsonMode?args.value():read()},signal:abort.signal});await renderFullJson(result,value,{signal:abort.signal})}catch(x){err.textContent=x.message;err.hidden=false;result.textContent='执行失败'}finally{submit.disabled=false}};const popup=dialog('测试 · '+tool.name,form);popup.addEventListener('close',()=>abort.abort(),{once:true})}

export function transportTemplate(transport){const common={startup_timeout:30,call_timeout:60,stop_timeout:10,queue_timeout:30,concurrency:1,idle_seconds:86400};if(transport==='stdio')return{...common,command:'uv',args:[],env:{},cwd:null,encoding:'utf-8'};if(transport==='rest')return{...common,tools:[],headers:{},verify_tls:true,auth:{type:'none'}};return{...common,url:'',headers:{},verify_tls:true,auth:{type:'none'}}
}

export const credentialStateLabel=state=>({not_required:'无需配置',missing:'未配置',pending_authorization:'待授权',expired:'授权已过期',needs_reauth:'需要重新授权',discovering:'发现中',ready:'可用',failed:'发现失败'}[state]||state);

function personalRefreshControls(service,initial,onChange){
 if(!initial.refresh_supported)return null;
 const root=el('section',{class:'stack personal-capability-refresh'});
 const draw=(status,message='',failed=false)=>{
  const changed=status.revision!==service.revision,control=button('刷新能力缓存',async()=>{
   let next=status,text='',error=false;
   try{
    const value=await api('/mcps/'+service.id+'/credentials/refresh',{method:'POST',body:{revision:service.revision}});
    next=value;text='已刷新：工具 '+value.tool_count+' · 资源 '+value.resource_count+' · 提示词 '+value.prompt_count+' · 资源模板 '+value.template_count;
   }catch(e){
    text=e.message;error=true;
    try{next=await api('/mcps/'+service.id+'/credentials/status')}
    catch(failure){next={...status,refresh_allowed:false,refresh_reason:failure.message}}
   }
   if(root.isConnected)draw(next,text,error);
   await onChange()
  });
  control.disabled=changed||!status.refresh_allowed;
  root.replaceChildren(...[el('p',{class:'muted'},'仅刷新你自己的能力缓存，不会保存表单中尚未保存的修改。'),control,
   changed?el('p',{class:'muted'},'服务配置已变化，请关闭后重新打开。'):status.refresh_reason?el('p',{class:'muted'},status.refresh_reason):null,
   message?el('div',{class:failed?'error':'notice',role:failed?'alert':'status'},message):null].filter(Boolean))
 };
 draw(initial);return root
}

export async function credentialDialog(service,onChange=()=>{}){
 const status=await api('/mcps/'+service.id+'/credentials/status'),authType=status.auth_type;
 const defs={
  bearer:[['token','Bearer Token','password']],
  basic:[['username','用户名','text'],['password','密码','password']],
  api_key:[['header','请求头名称（例如 X-API-Key）','text'],['value','API Key','password']],
  oauth:[['authorization_url','授权 URL','url'],['token_url','Token URL','url'],['client_id','Client ID','text'],['client_secret','Client Secret','password'],['scopes','Scopes（空格分隔）','text']]
 }[authType];
 if(!defs)throw new Error('此服务无需配置个人凭据');
 if(authType==='oauth'){
  const oauth=oauthFields(status.oauth_config||{}, {allowAutomatic:['streamable-http','sse'].includes(service.transport)});
  formDialog('OAuth 配置',[oauth.node,personalRefreshControls(service,status,onChange)],async()=>{
   const auth={...oauth.value(),config_isolation:'user'};
   const next=await api('/mcps/'+service.id+'/credentials',{method:'PUT',body:{revision:service.revision,auth}});
   toast('个人凭据已保存，状态：'+credentialStateLabel(next.state),'success');onChange();
  });return;
 }
 const fields=Object.fromEntries(defs.map(([key,label,type])=>[key,field(label,key==='header'?'X-API-Key':'',type)]));
 for(const[key]of defs)fields[key].input.required=true;
 formDialog('配置凭据',[...defs.map(([key])=>fields[key].node),personalRefreshControls(service,status,onChange)],async()=>{
  const auth={type:authType};
  for(const[key]of defs)auth[key]=fields[key].input.value;
  const next=await api('/mcps/'+service.id+'/credentials',{method:'PUT',body:{revision:service.revision,auth}});
  toast('个人凭据已保存，状态：'+credentialStateLabel(next.state),'success');onChange();
 });
}

export async function oauthDialog(service,canManage,onChange=()=>{},returnTo='mcps'){
 const state=await api('/mcps/'+service.id+'/oauth/status'),content=el('div',{class:'stack'});
 content.append(el('p',{class:'notice'},state.authorized?'当前登录用户已完成 OAuth 授权。':state.state==='needs_reauth'?'授权已失效，请点击重新授权以继续使用。':'当前登录用户尚未完成 OAuth 授权，或授权已过期。其他用户的个人授权不可复用。'));
 content.append(el('p',{class:'muted'},'授权不会自动启用已禁用的服务。授权返回后点击“重新检查授权状态”，可在个人资料中刷新本人的能力缓存；管理员也可验证并启动。读取工具成功后才恢复可用目录。列表中的失败时间表示上一次尝试。'));
 const d=dialog('授权服务',content);
 if(returnTo==='profile'){
  try{const personal=await api('/mcps/'+service.id+'/credentials/status');if(d.isConnected)content.append(...[personalRefreshControls(service,personal,onChange)].filter(Boolean))}
  catch(error){if(d.isConnected)content.append(el('p',{class:'error',role:'alert'},error.message))}
 }
 content.append(button('重新检查授权状态',async()=>{d.close();await oauthDialog(service,canManage,onChange,returnTo)}));
 if(state.authorized&&canManage)content.append(button('验证并启动',async()=>{try{await api('/mcps/'+service.id+'/start',{method:'POST'});toast('验证成功，服务已启动','success');d.close()}finally{onChange()}},'primary'));
 if(state.authorized)content.append(button('断开 OAuth 授权',()=>formDialog('确认操作',[el('p',{},'断开后，你需要重新授权才能调用此服务。其他用户的授权不受影响。')],async()=>{await api('/mcps/'+service.id+'/oauth/disconnect',{method:'POST'});d.close();toast('已断开 OAuth 授权');onChange()},{saveLabel:'确认执行'}),'danger'));
 const authorize=async()=>{try{const result=await api('/mcps/'+service.id+'/oauth/start',{method:'POST',body:{return_to:returnTo}}),url=new URL(result.authorization_url);if(!['http:','https:'].includes(url.protocol))throw new Error('授权地址不受支持');if(d.isConnected)content.append(el('a',{href:url.href,target:'_blank',rel:'noopener noreferrer'},'打开授权页面'))}catch(e){if(d.isConnected)content.append(el('p',{class:'error',role:'alert'},e.message))}};content.append(button(state.authorized||state.state==='needs_reauth'?'重新授权':'开始授权',authorize,'primary'));
}
