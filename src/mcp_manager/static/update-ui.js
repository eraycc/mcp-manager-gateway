import{el,button,check}from './core.js';

export function notificationDot(count=1){
 return el('span',{class:'notification-dot','aria-hidden':'true'},String(count));
}

export function syncUpdateNotifications(count=0){
 const targets=[
  document.querySelector('.nav-link[href="#/settings"]'),
  document.querySelector('#settings_tab-tab-about .tab-label'),
 ].filter(Boolean);
 for(const target of targets){
  target.querySelector(':scope > .notification-dot')?.remove();
  if(count)target.append(notificationDot(count));
 }
}

export function updateCheckSetting(enabled=true){
 const control=check('自动检测更新',enabled);
 return{
  node:el('section',{class:'update-setting stack'},control.node,el('small',{class:'muted'},'开启后登录时自动通过 PyPI 检查；关闭后仍可使用“检查更新”。')),
  value:()=>control.input.checked,
 };
}

export function updateAboutPanel({about,state,onCheck,onIgnore,autoCheckNode}){
 const current=state||{current_version:about.version,latest_version:'',update_available:false,notification_count:0,pypi_url:about.pypi_url,releases_url:about.releases_url};
 const link=(label,url)=>el('a',{href:url,target:'_blank',rel:'noopener noreferrer',class:'about-link'},label);
 const status=current.check_error
  ?el('p',{class:'error',role:'alert'},'更新检测失败：'+current.check_error)
  :current.update_available
   ?el('p',{class:'notice update-notice'},'发现新版本 '+current.latest_version+'，请根据当前安装方式完成升级。')
   :el('p',{class:'muted'},current.latest_version?'当前已经是最新版本。':'尚未检查最新版本。');
 return el('section',{class:'card about-card'},
  el('div',{class:'about-heading'},
   el('div',{},el('h2',{},'MCP Manager Gateway'),el('p',{class:'muted'},'集中管理 MCP 服务与客户端连接。')),
   el('div',{class:'about-versions'},
    el('div',{class:'about-version'},el('span',{class:'muted'},'当前版本'),el('strong',{},about.version)),
    el('div',{class:'about-version'},el('span',{class:'muted'},'最新版本'),el('strong',{},current.latest_version||'—'))
   )
  ),
  autoCheckNode,
  status,
  el('div',{class:'actions'},button('检查更新',onCheck),current.update_available?button('跳过本次更新',onIgnore):null),
  el('div',{class:'about-links'},
   link('项目主页',about.project_url),
   link('GitHub Releases',current.releases_url||about.releases_url),
   link('PyPI 发布页',current.pypi_url||about.pypi_url),
   link('作者 '+about.author,about.author_url),
   link('问题反馈',about.issues_url)
  )
 );
}
