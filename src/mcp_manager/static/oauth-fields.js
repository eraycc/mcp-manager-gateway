import{el,field,selectField}from './core.js';
import{cimdFields}from './cimd-fields.js';

const runtimeFields=['access_token','refresh_token','expires_at','expires_in','id_token','token_type','granted_scope','client_secret_expires_at','client_id_issued_at','registration_access_token'];
export function oauthFields(initial={}, {secretsVisible=false,allowAutomatic=true}={}){
 let selected=initial.auth_mode;
 if(selected==='manual')selected='pre_registration';
 else if(selected==='auto')selected=initial.allow_dcr===true?'prefer_cimd_fallback_dcr':'cimd_only';
 else if(selected==='dcr')selected='dcr_only';
 else if(!selected)selected=initial.client_id||initial.authorization_url||initial.token_url?'pre_registration':'prefer_cimd_fallback_dcr';
 if(!allowAutomatic)selected='pre_registration';
 const mode=selectField('OAuth 客户端注册方式',selected,
  [['pre_registration','预注册（手动 Client ID / Secret）'],...(allowAutomatic?[
   ['prefer_cimd_fallback_dcr','自动（优先 CIMD，失败回退 DCR）'],
   ['cimd_only','自动（仅 CIMD）'],['dcr_only','自动（仅 DCR）']]:[])]);
 const modeHelp=el('div',{class:'stack muted span2',style:'gap:0'},
  ...[
   '预注册：手动填写已在目标服务注册的 Client ID / Secret，不发起自动注册。',
   'CIMD：用公网 HTTPS 元数据 URL 作为客户端标识。可由 MMG 托管，也可单独发布静态 JSON，让 MMG 保留在本机或内网。',
   'DCR：动态客户端注册，MMG 直接向服务器注册并获取 Client ID/Secret，不需要公网地址。',
   '自动（优先 CIMD，失败回退 DCR）（默认）：先尝试 CIMD，前置条件不满足或收到明确的客户端/元数据拒绝错误时回退 DCR。',
   '自动（仅 CIMD）：只使用 CIMD，失败时报错，不自动回退。',
   '自动（仅 DCR）：只使用 DCR，不尝试 CIMD。',
   '如果授权页面报错且没有回调 MMG，无法自动检测失败，可切换“自动（仅 DCR）”重试。用户拒绝授权、状态校验失败或 Token 请求超时不会自动回退。',
   'Figma 等部分服务器可能要求特定 client_name 或回调路径，请按服务端要求调整。'
  ].map(text=>el('small',{},text)));
 const id=field('Client ID',initial.client_id||'','text','预注册模式必填。'),
  secret=field('Client Secret',initial.client_secret||'',secretsVisible?'text':'password','可选：公共客户端可不填，使用 PKCE。'),
  name=field('客户端名称（client_name）',initial.client_name||'Codex','text','DCR 注册必填，默认 Codex；这是客户端元数据，不代表获得 Codex 身份，也不保证服务器接受。');
 const method=selectField('Token 端点认证方式',initial.token_endpoint_auth_method||'',
  [['','自动选择'],['none','none（公共客户端）'],['client_secret_basic','client_secret_basic'],['client_secret_post','client_secret_post']]);
 const automatic=el('div',{class:'stack span2'},name.node);
 const manual=el('div',{class:'form-grid span2'},id.node,secret.node);
 const methodHelp=el('p',{class:'muted span2'},'自动选择时按服务器能力协商；DCR 返回的 Secret 加密保存。CIMD 仅支持 none，选 Secret 方式时优先模式回退 DCR，仅 CIMD 模式报不兼容。');
 for(const[label,f]of [['Client ID',id],['Client Secret',secret],['客户端名称（client_name）',name]])f.input.setAttribute('aria-label',label);
 const endpoints={};
 for(const [key,label]of [['authorization_url','授权 URL'],['token_url','Token URL'],['registration_url','注册 URL'],['issuer_url','Issuer URL']]){
  const f=field(label,initial[key]||'','url','可选，优先使用服务器发现信息；发现不可用时可手动填写端点。');f.input.setAttribute('aria-label',label);endpoints[key]=f;
 }
 const callback=selectField('OAuth 回调路径',initial.callback_path||'/api/v1/oauth/callback',
  [['/api/v1/oauth/callback','/api/v1/oauth/callback（默认）'],['/callback','/callback']]);
 callback.node.append(el('small',{},'OAuth 回调路径。大多数服务使用默认的 /api/v1/oauth/callback；Figma 等部分远程 MCP 服务要求使用 /callback，若授权时提示 invalid_redirect_uri，请切换到 /callback 重试。'));
 const scopes=field('Scopes（空格分隔）',Array.isArray(initial.scopes)?initial.scopes.join(' '):initial.scopes||'','text',el('span',{},'用于声明申请的权限范围，多个权限用空格分隔（如 read write mcp:connect）。',el('br'),'留空时由服务器发现信息自动确定；Figma 等远程 MCP 通常填 mcp:connect。'));
 scopes.input.setAttribute('aria-label','Scopes（空格分隔）');
 const cimd=cimdFields(initial,{callback:callback.input,mode:mode.input});
 function sync(clear=false){
  const auto=mode.input.value!=='pre_registration',dcr=['prefer_cimd_fallback_dcr','dcr_only'].includes(mode.input.value);
  if(clear&&auto){id.input.value='';secret.input.value=''}
  if(dcr&&!name.input.value)name.input.value='Codex';
  manual.hidden=auto;automatic.hidden=!dcr;
  id.input.required=!auto;secret.input.required=false;name.input.required=dcr;
  id.input.disabled=auto;secret.input.disabled=auto;name.input.disabled=!dcr;cimd.sync();
 }
 mode.input.addEventListener('change',()=>sync(true));sync();
 const node=el('div',{class:'form-grid span2 oauth-fields'},mode.node,modeHelp,manual,automatic,method.node,methodHelp,...Object.values(endpoints).map(f=>f.node),callback.node,scopes.node,cimd.node);
 return{node,value(validate=true){
  const auth={...initial,type:'oauth',auth_mode:mode.input.value};
  for(const key of [...runtimeFields,'client_id','client_secret','client_name','allow_dcr','token_endpoint_auth_method','callback_path',...Object.keys(endpoints)])delete auth[key];
  if(mode.input.value==='pre_registration'){
   if(validate&&!id.input.value.trim())throw new Error('预注册模式必须填写 Client ID');
   auth.client_id=id.input.value.trim();if(secret.input.value)auth.client_secret=secret.input.value;
  }else if(['prefer_cimd_fallback_dcr','dcr_only'].includes(mode.input.value)){
   if(validate&&!name.input.value.trim())throw new Error('DCR 注册必须填写客户端名称');
   auth.client_name=name.input.value.trim();
  }
  if(method.input.value)auth.token_endpoint_auth_method=method.input.value;
  if(callback.input.value!=='/api/v1/oauth/callback')auth.callback_path=callback.input.value;
  for(const[key,f]of Object.entries(endpoints))if(f.input.value.trim())auth[key]=f.input.value.trim();
  for(const key of Object.keys(auth))if(key.startsWith('cimd_'))delete auth[key];
  Object.assign(auth,cimd.value(validate));
  auth.scopes=scopes.input.value.split(/[ ,]+/).filter(Boolean);
  return auth;
 }};
}
