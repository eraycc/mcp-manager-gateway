import{el,field,check,selectField}from './core.js';

const runtimeFields=['access_token','refresh_token','expires_at','expires_in','id_token','token_type','granted_scope','registration_access_token'];
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
 const manual=el('div',{class:'form-grid span2'},id.node,secret.node,method.node);
 for(const[label,f]of [['Client ID',id],['Client Secret',secret],['客户端名称（client_name）',name]])f.input.setAttribute('aria-label',label);
 const endpoints={};
 for(const [key,label]of [['authorization_url','授权 URL'],['token_url','Token URL'],['registration_url','注册 URL'],['issuer_url','Issuer URL']]){
  const f=field(label,initial[key]||'','url','可选，优先使用服务器发现信息；发现不可用时可手动填写端点。');f.input.setAttribute('aria-label',label);endpoints[key]=f;
 }
 const scopes=field('Scopes（空格分隔）',Array.isArray(initial.scopes)?initial.scopes.join(' '):initial.scopes||'');
 function sync(clear=false){
  const auto=mode.input.value==='auto';
  if(clear){if(auto){id.input.value='';secret.input.value='';method.input.value=''}else{dcr.input.checked=false;name.input.value=''}}
  if(auto&&dcr.input.checked&&!name.input.value)name.input.value='Codex';
  if(!auto||!dcr.input.checked)name.input.value='';
  manual.hidden=auto;automatic.hidden=!auto;name.node.hidden=!auto||!dcr.input.checked;
  id.input.required=!auto;secret.input.required=false;name.input.required=auto&&dcr.input.checked;
  id.input.disabled=auto;secret.input.disabled=auto;method.input.disabled=auto;name.input.disabled=!auto||!dcr.input.checked;
 }
 mode.input.addEventListener('change',()=>sync(true));dcr.input.addEventListener('change',()=>sync());sync();
 const node=el('div',{class:'form-grid span2 oauth-fields'},mode.node,manual,automatic,...Object.values(endpoints).map(f=>f.node),scopes.node);
 return{node,value(validate=true){
  const auth={...initial,type:'oauth',auth_mode:mode.input.value};
  for(const key of [...runtimeFields,'client_id','client_secret','client_name','allow_dcr','token_endpoint_auth_method',...Object.keys(endpoints)])delete auth[key];
  if(mode.input.value==='manual'){
   if(validate&&!id.input.value.trim())throw new Error('预注册模式必须填写 Client ID');
   auth.client_id=id.input.value.trim();if(secret.input.value)auth.client_secret=secret.input.value;
   if(method.input.value)auth.token_endpoint_auth_method=method.input.value;
  }else{
   auth.allow_dcr=dcr.input.checked;
   if(auth.allow_dcr){if(validate&&!name.input.value.trim())throw new Error('DCR 注册必须填写客户端名称');auth.client_name=name.input.value.trim()}
  }
  for(const[key,f]of Object.entries(endpoints))if(f.input.value.trim())auth[key]=f.input.value.trim();
  auth.scopes=scopes.input.value.split(/[ ,]+/).filter(Boolean);
  return auth;
 }};
}
