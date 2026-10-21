import{el,field,check,selectField}from './core.js';

const runtimeFields=['access_token','refresh_token','expires_at','expires_in','id_token','token_type','granted_scope','client_secret_expires_at','client_id_issued_at','registration_access_token'];
export function oauthFields(initial={}, {secretsVisible=false,allowAutomatic=true}={}){
 const mode=selectField('OAuth 客户端注册方式',initial.auth_mode==='auto'||initial.auth_mode==='dcr'?'auto':'manual',
  [['manual','预注册（手动 Client ID / Secret）'],...(allowAutomatic?[['auto','自动选择（CIMD 优先，DCR 可选回退）']]:[])]);
 const dcr=check('允许 DCR 回退（仅服务器不支持 CIMD 时）',initial.auth_mode==='dcr'||initial.allow_dcr===true);
 const id=field('Client ID',initial.client_id||'','text','预注册模式必填。'),
  secret=field('Client Secret',initial.client_secret||'',secretsVisible?'text':'password','可选：公共客户端可不填，使用 PKCE。'),
  name=field('客户端名称（client_name）',initial.client_name||'','text','DCR 注册必填，默认 Codex；这是客户端元数据，不代表获得 Codex 身份，也不保证服务器接受。');
 const method=selectField('Token 端点认证方式',initial.token_endpoint_auth_method||'',
  [['','自动选择'],['none','none（公共客户端）'],['client_secret_basic','client_secret_basic'],['client_secret_post','client_secret_post']]);
 const automatic=el('div',{class:'stack span2'},el('p',{class:'muted'},'服务器支持 CIMD 时，使用 MMG 托管的 /oauth/client-metadata.json，需要稳定、可公开访问的 HTTPS PUBLIC_URL。仅当服务器不支持 CIMD 且提供注册端点时，才可回退 DCR。'),dcr.node,name.node);
 const manual=el('div',{class:'form-grid span2'},id.node,secret.node);
 const methodHelp=el('p',{class:'muted span2'},'自动注册时按服务器能力选择 DCR 认证方式，Secret 由注册返回并加密保存；可指定 none、client_secret_basic 或 client_secret_post。CIMD 当前仅支持 none，选择 Secret 认证时会明确提示不兼容。');
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
 function sync(clear=false){
  const auto=mode.input.value==='auto';
  if(clear){if(auto){id.input.value='';secret.input.value=''}else{dcr.input.checked=false;name.input.value=''}}
  if(auto&&dcr.input.checked&&!name.input.value)name.input.value='Codex';
  if(!auto||!dcr.input.checked)name.input.value='';
  manual.hidden=auto;automatic.hidden=!auto;name.node.hidden=!auto||!dcr.input.checked;
  id.input.required=!auto;secret.input.required=false;name.input.required=auto&&dcr.input.checked;
  id.input.disabled=auto;secret.input.disabled=auto;name.input.disabled=!auto||!dcr.input.checked;
 }
 mode.input.addEventListener('change',()=>sync(true));dcr.input.addEventListener('change',()=>sync());sync();
 const node=el('div',{class:'form-grid span2 oauth-fields'},mode.node,manual,automatic,method.node,methodHelp,...Object.values(endpoints).map(f=>f.node),callback.node,scopes.node);
 return{node,value(validate=true){
  const auth={...initial,type:'oauth',auth_mode:mode.input.value};
  for(const key of [...runtimeFields,'client_id','client_secret','client_name','allow_dcr','token_endpoint_auth_method','callback_path',...Object.keys(endpoints)])delete auth[key];
  if(mode.input.value==='manual'){
   if(validate&&!id.input.value.trim())throw new Error('预注册模式必须填写 Client ID');
   auth.client_id=id.input.value.trim();if(secret.input.value)auth.client_secret=secret.input.value;
  }else{
   auth.allow_dcr=dcr.input.checked;
   if(auth.allow_dcr){if(validate&&!name.input.value.trim())throw new Error('DCR 注册必须填写客户端名称');auth.client_name=name.input.value.trim()}
  }
  if(method.input.value)auth.token_endpoint_auth_method=method.input.value;
  if(callback.input.value!=='/api/v1/oauth/callback')auth.callback_path=callback.input.value;
  for(const[key,f]of Object.entries(endpoints))if(f.input.value.trim())auth[key]=f.input.value.trim();
  auth.scopes=scopes.input.value.split(/[ ,]+/).filter(Boolean);
  return auth;
 }};
}
