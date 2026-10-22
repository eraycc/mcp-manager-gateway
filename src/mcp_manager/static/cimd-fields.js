import{el,field,selectField,button,api,download,pretty}from './core.js';

export function cimdFields(initial,{callback,mode,clientName}){
 const source=selectField('CIMD 元数据托管方式',initial.cimd_metadata_source||'mmg',
  [['mmg','MMG 托管'],['external','外部静态托管（内网可用）']]);
 const address=field('客户端元数据托管 URL',initial.cimd_external_url||'','url',
  '最终直接返回 JSON 的公网 HTTPS 地址，例如 https://username.github.io/client-metadata.json；不限定文件名，不支持 HTTP 跳转。');
 address.input.setAttribute('aria-label','客户端元数据托管 URL');address.input.spellcheck=false;
 const editor=field('元数据 JSON',initial.cimd_metadata?pretty(initial.cimd_metadata):'','textarea',
  'client_id 必须与上方托管的 URL 逐字符一致；redirect_uris 必须包含上方“MMG 实际回调地址”中的完整回调地址。编辑 JSON 不会改变 MMG 实际使用的回调地址。');
 editor.input.setAttribute('aria-label','元数据 JSON');editor.input.className='json';editor.input.spellcheck=false;
 const preview=el('details',{class:'cimd-preview'},el('summary',{},'预览／编辑元数据 JSON'),editor.node);
 const status=el('div',{class:'cimd-status muted',role:'status','aria-live':'polite'},'未验证线上文件');
 const callbackValue=el('code',{class:'cimd-callback'},'正在读取回调配置…');
 const callbackInfo=el('div',{class:'cimd-callback-info'},el('strong',{},'MMG 实际回调地址'),callbackValue);
 const warning=el('p',{class:'notice cimd-warning',hidden:true});
 const hostingHelp=el('p',{class:'notice'});
 const currentName=()=>clientName.value.trim()||'Codex';
 let epoch=0,contextEpoch=0,loaded=false,active=false,context=null,generatedKey=null;
 const key=()=>JSON.stringify([source.input.value,address.input.value,callback.value,editor.input.value]);
 const activeCimd=()=>['prefer_cimd_fallback_dcr','cimd_only'].includes(mode.value);
 const external=()=>activeCimd()&&source.input.value==='external';
 function mark(text,failed=false){
  status.textContent=text;status.className='cimd-status '+(failed?'error':'muted');
  status.setAttribute('role',failed?'alert':'status');
 }
 function stale(){
  epoch++;mark(editor.input.value?'已过期：配置或编辑内容已变化，请发布当前 JSON 后重新验证。':'未验证线上文件');
 }
 function data(){
  let metadata;
  if(editor.input.value.trim()){
   try{metadata=JSON.parse(editor.input.value)}catch{throw new Error('元数据 JSON 格式不正确，请修正后重试')}
   if(!metadata||Array.isArray(metadata)||typeof metadata!=='object')throw new Error('元数据 JSON 必须是对象');
   const forbidden=new Set(['client_secret','client_secret_expires_at','access_token','refresh_token','id_token','registration_access_token','private_key','d','p','q','dp','dq','qi','oth','k']);
   const inspect=value=>{
    if(Array.isArray(value)){for(const item of value)inspect(item)}
    else if(value&&typeof value==='object'){for(const [name,item]of Object.entries(value)){
     if(forbidden.has(name.toLowerCase()))throw new Error('公开元数据不能包含 Secret、Token 或私钥，请先移除敏感字段');
     inspect(item);
    }}else if(typeof value==='number'&&!Number.isFinite(value))throw new Error('元数据数值必须是有限 JSON 数字');
   };
   inspect(metadata);
  }
  return{source:'external',url:address.input.value,callback_path:callback.value,...(metadata?{metadata}:{})};
 }
 function showResult(result){
  if(result.ok)mark('已验证线上文件 · '+new Date(result.verified_at*1000).toLocaleString()+'；此结果不代表目标服务已接受或已完成授权。');
  else if(result.status==='stale')mark('验证已过期，将在开始授权时重新验证。');
  else if(result.status==='failed')mark('['+result.code+'] '+result.message,true);
  else mark('未验证线上文件；开始授权时 MMG 会自动验证。');
 }
 async function refreshStatus(){
  if(!external()||!address.input.value)return;
  const version=++epoch,snapshot=key();
  try{
   const result=await api('/oauth/client-metadata/status',{method:'POST',body:data()});
   if(version===epoch&&snapshot===key()&&external())showResult(result);
  }catch(error){if(version===epoch)mark(error.message,true)}
 }
 async function loadContext(){
  const version=++contextEpoch,path=callback.value;
  try{
   const result=await api('/oauth/client-metadata/context?'+new URLSearchParams({callback_path:path}));
   if(version!==contextEpoch)return;
   context=result;callbackValue.textContent=result.redirect_uri||result.public_url||'未配置 PUBLIC_URL';
   const hints=[];
   if(result.message)hints.push(result.message);
   if(result.redirect_uri){
    const expected=new URL(result.redirect_uri);
    if(expected.hostname!==location.hostname)hints.push('当前控制台主机与回调主机不同，请先使用回调地址对应的 MMG 主机登录，再在同一浏览器完成授权。');
    hints.push('回调地址必须能被发起授权的浏览器访问；127.0.0.1 仅适用于浏览器与 MMG 同机的情况。跨设备访问时，请使用浏览器可达的地址（如内网 HTTPS 地址或公网域名）。');
   }
   warning.textContent=hints.join(' ');warning.hidden=!hints.length;
  }catch(error){if(version===contextEpoch){callbackValue.textContent='无法读取回调配置';warning.textContent=error.message;warning.hidden=false}}
 }
 const generate=button('生成 JSON',async()=>{
  const version=++epoch,snapshot=key(),path=callback.value,name=currentName();
  try{
   const value=await api('/oauth/client-metadata/template?'+new URLSearchParams({source:'external',hosting_url:address.input.value,callback_path:path,client_name:name}));
   if(version!==epoch||snapshot!==key()||name!==currentName()||!external())return;
   editor.input.value=pretty(value);generatedKey=JSON.stringify([address.input.value,path]);
   preview.open=true;mark('JSON 已生成，请复制或下载，发布到上方 URL 后验证线上文件。');
  }catch(error){if(version===epoch)mark(error.message,true)}
 });
 function exportText(){
  const payload=data();
  if(!payload.metadata)throw new Error('请先生成或填写元数据 JSON');
  if(generatedKey&&generatedKey!==JSON.stringify([address.input.value,callback.value]))throw new Error('托管 URL 或回调已变化，请重新生成或修正 JSON');
  if(payload.metadata.client_id!==address.input.value)throw new Error('JSON 中 client_id 必须与托管 URL 一致');
  return pretty(payload.metadata);
 }
 const copy=button('复制 JSON',async()=>{
  try{
   const value=exportText();
   if(!navigator.clipboard?.writeText){preview.open=true;editor.input.focus();editor.input.select();mark('浏览器不支持直接复制，已选中 JSON，请使用系统复制操作。');return}
   await navigator.clipboard.writeText(value);mark('已复制 JSON，发布后请验证线上文件。');
  }catch(error){mark(error.message,true)}
 });
 const save=button('下载 JSON',()=>{try{download('client-metadata.json',exportText())}catch(error){mark(error.message,true)}});
 const verify=button('验证线上文件',async()=>{
  const version=++epoch,snapshot=key();mark('正在验证线上文件…');
  try{
   const result=await api('/oauth/client-metadata/verify',{method:'POST',body:data()});
   if(version===epoch&&snapshot===key()&&external())showResult(result);
  }catch(error){if(version===epoch)mark(error.message,true)}
 },'primary');
 const externalBox=el('div',{class:'stack cimd-external'},
  el('p',{class:'muted'},'填写 URL → 生成并发布 JSON → 验证线上文件'),
  address.node,el('div',{class:'cimd-actions'},generate,copy,save,verify),status,preview,
  el('small',{class:'muted'},'文件应允许匿名读取并返回 application/json，不要发布 Token、Secret 或私钥。验证只确认 MMG 能读取且内容匹配；目标授权服务器仍可能有额外限制。'));
 const node=el('section',{class:'stack span2 cimd-fields'},source.node,
  hostingHelp,
  callbackInfo,warning,externalBox);
 source.input.addEventListener('change',()=>{stale();sync();refreshStatus()});
 address.input.addEventListener('input',stale);address.input.addEventListener('blur',refreshStatus);
 editor.input.addEventListener('input',()=>{generatedKey=null;stale()});
 callback.addEventListener('change',()=>{stale();loadContext();refreshStatus()});
 function sync(){
  hostingHelp.textContent=source.input.value==='external'
   ?'需要生成一份静态 client-metadata.json 并部署至 GitHub Pages、Vercel 等平台，MMG 可留在本机或内网。填写部署后的 HTTPS 地址（即 client_id），生成并部署文件后点击验证。回调仍须由同一已登录浏览器访问。'
   :'由 MMG 在 {PUBLIC_URL}/oauth/client-metadata.json 提供元数据，要求 PUBLIC_URL 为稳定、可公开访问的 HTTPS 地址，需在启动前通过环境变量设置。若 PUBLIC_URL 为 http://127.0.0.1:8765 或内网地址，CIMD 将不可用，请改用“外部托管”或回退 DCR。';
  active=activeCimd();node.hidden=!active;externalBox.hidden=!external();
  address.input.disabled=!external();address.input.required=external();editor.input.disabled=!external();
  if(active&&!loaded){loaded=true;loadContext();refreshStatus()}
 }
 sync();
 return{node,sync,value(validate=true){
  const result={};
  if(source.input.value==='external'||initial.cimd_metadata_source){
   result.cimd_metadata_source=source.input.value;
  }
  if(address.input.value)result.cimd_external_url=address.input.value;
  if(external()&&validate){
   if(!address.input.value)throw new Error('请填写客户端元数据托管 URL');
   const u=new URL(address.input.value);
   if(u.protocol!=='https:'||!u.pathname||u.username||u.password||u.hash)throw new Error('元数据托管 URL 必须是带路径的 HTTPS 地址，且不含凭据或 fragment');
  }
  try{if(editor.input.value.trim())result.cimd_metadata=data().metadata}
  catch(error){if(external()&&validate)throw error;else if(initial.cimd_metadata)result.cimd_metadata=initial.cimd_metadata}
  return result;
 }};
}
