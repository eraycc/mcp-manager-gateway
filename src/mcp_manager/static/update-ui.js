import{el,button,check,dialog}from './core.js';

const UPDATE_CACHE_KEY='mcp-update-state';
const UPDATE_NOTICE_KEY='mcp-update-notice';

function cachedShape(state={}){
 return{
  current_version:String(state.current_version||''),
  latest_version:String(state.latest_version||''),
  ignored_version:String(state.ignored_version||''),
  checked_at:state.checked_at||null,
  update_available:!!state.update_available,
  notification_count:state.update_available?1:0,
  check_error:'',
 };
}

export function readCachedUpdateState(){
 try{
  const value=JSON.parse(localStorage.getItem(UPDATE_CACHE_KEY)||'null');
  return value&&typeof value==='object'?cachedShape(value):undefined;
 }catch{return undefined}
}

export function cacheUpdateState(state){
 const value=cachedShape(state);
 try{localStorage.setItem(UPDATE_CACHE_KEY,JSON.stringify(value))}catch{}
 return value;
}

export function notificationDot(count=1){
 return el('span',{class:'notification-dot','aria-hidden':'true'},String(count));
}

export function syncUpdateNotifications(count=0,state){
 const targets=[
  document.querySelector('.nav-link[href="#/settings"]'),
  document.querySelector('#settings_tab-tab-about .tab-label'),
 ].filter(Boolean);
 for(const target of targets){
  target.querySelector(':scope > .notification-dot')?.remove();
  if(count)target.append(notificationDot(count));
 }
 if(state)document.querySelectorAll('.about-card').forEach(panel=>panel.syncUpdateState?.(state));
}

export function maybeNotifyUpdate(state){
 if(!state?.update_available||!state.latest_version)return false;
 const marker=String(state.current_version||'')+'->'+String(state.latest_version);
 try{
  if(localStorage.getItem(UPDATE_NOTICE_KEY)===marker)return false;
  localStorage.setItem(UPDATE_NOTICE_KEY,marker);
 }catch{}
 dialog('发现新版本',el('div',{class:'stack update-dialog'},
  el('p',{},'MCP Manager Gateway '+state.latest_version+' 已发布。'),
  el('p',{class:'muted'},'当前版本：'+(state.current_version||'未知')+'。可前往“系统设置 → 关于”查看升级入口。')
 ));
 return true;
}

export function updateCheckSetting(enabled=true){
 const control=check('自动检测更新',enabled);
 return{
  node:el('section',{class:'update-setting stack'},control.node,el('small',{class:'muted'},'开启后登录时自动通过 PyPI 检查；关闭后仍可使用“检查更新”。')),
  value:()=>control.input.checked,
 };
}

function statusNode(current){
 if(current.check_error)return el('p',{class:'update-status update-status-error',role:'alert'},'更新检测失败：'+current.check_error);
 if(current.update_available)return el('p',{class:'update-status update-status-outdated'},'发现新版本 '+current.latest_version+'，请根据当前安装方式完成升级。');
 if(current.latest_version&&current.ignored_version===current.latest_version)return el('p',{class:'update-status muted'},'已忽略版本 '+current.latest_version+'，仍可随时手动检查更新。');
 if(current.latest_version)return el('p',{class:'update-status update-status-current'},'当前已经是最新版本。');
 return el('p',{class:'update-status muted'},'尚未检查最新版本。');
}

export function updateAboutPanel({about,state,onCheck,onIgnore,autoCheckNode}){
 let current=state||{current_version:about.version,latest_version:'',update_available:false,notification_count:0};
 const link=(label,url)=>el('a',{href:url,target:'_blank',rel:'noopener noreferrer',class:'about-link'},label);
 const latest=el('strong',{},current.latest_version||'—'),status=el('div'),actions=el('div',{class:'actions'});
 const panel=el('section',{class:'card about-card'},
  el('div',{class:'about-heading'},
   el('div',{},el('h2',{},'MCP Manager Gateway'),el('p',{class:'muted'},'集中管理 MCP 服务与客户端连接。')),
   el('div',{class:'about-versions'},
    el('div',{class:'about-version'},el('span',{class:'muted'},'当前版本'),el('strong',{},about.version)),
    el('div',{class:'about-version'},el('span',{class:'muted'},'最新版本'),latest)
   )
  ),
  autoCheckNode,
  status,
  actions,
  el('div',{class:'about-links'},
   link('项目主页',about.project_url),
   link('GitHub Releases',about.releases_url),
   link('PyPI 发布页',about.pypi_url),
   link('作者 '+about.author,about.author_url),
   link('问题反馈',about.issues_url)
  )
 );
 panel.syncUpdateState=next=>{
  current=next||current;
  latest.textContent=current.latest_version||'—';
  status.replaceChildren(statusNode(current));
  actions.replaceChildren(button('检查更新',onCheck),...(current.update_available?[button('跳过本次更新',onIgnore)]:[]));
 };
 panel.syncUpdateState(current);
 return panel;
}
