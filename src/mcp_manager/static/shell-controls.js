import{el,icon,button,toast,run,navigate}from './core.js';
import{TRANSLATION_LANGUAGES,applyTranslation,preferredTranslationTarget}from './translation.js';

const themePreference=()=>localStorage.getItem('mcp-theme')||'system';

export function applyTheme(value=themePreference()){
 const dark=value==='dark'||(value==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);
 document.body.classList.toggle('dark',dark);
 document.documentElement.style.colorScheme=dark?'dark':'light';
}

export function setTheme(value){
 localStorage.setItem('mcp-theme',value);
 applyTheme(value);
}

export function toggleTheme(){
 setTheme(document.body.classList.contains('dark')?'light':'dark');
}

export function initializeTheme(){
 applyTheme();
 matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change',()=>{
  if(themePreference()==='system')applyTheme();
 });
}

function summary(name,label){
 return el('summary',{class:'icon-button','aria-label':label,title:label},icon(name));
}

function themeMenu(onRefresh){
 const box=el('details',{class:'action-menu'},summary(themePreference()==='dark'?'moon':themePreference()==='light'?'sun':'monitor','显示模式'));
 const menu=el('div',{class:'action-menu-panel',role:'menu'},el('strong',{},'显示模式'));
 for(const[value,label,name]of[['light','浅色','sun'],['dark','深色','moon'],['system','系统','monitor']]){
  const item=button(label,()=>{setTheme(value);box.open=false;onRefresh?.()},themePreference()===value?'active':'');
  item.prepend(icon(name));menu.append(item);
 }
 box.append(menu);return box;
}

function languageMenu(config){
 const box=el('details',{class:'action-menu'},summary('globe','切换语言'));
 const selected=preferredTranslationTarget(config);
 const input=el('select',{'aria-label':'目标语言'},...TRANSLATION_LANGUAGES.map(([value,label])=>el('option',{value},label)));
 input.value=selected;
 input.onchange=()=>run(async()=>{
  localStorage.setItem('mcp-translation-target',input.value);
  const result=await applyTranslation(config,input.value);
  result.warnings.forEach(message=>toast(message,'warning'));
  box.open=false;
 });
 box.append(el('div',{class:'action-menu-panel language-menu'},el('strong',{},'翻译为'),input));
 return box;
}

function accountMenu(me,isAdmin,onLogout){
 const box=el('details',{class:'action-menu account-menu'},summary('user','账户菜单'));
 const action=(label,fn,kind='')=>button(label,async()=>{box.open=false;await fn()},kind);
 const info=el('div',{class:'account-summary'},el('strong',{},me.username),el('span',{class:'muted'},isAdmin?'管理员':'用户'),me.email?el('span',{class:'muted'},me.email):null);
 box.append(el('div',{class:'action-menu-panel'},info,action('个人资料',()=>navigate('profile')),...(isAdmin?[action('系统设置',()=>navigate('settings'))]:[]),action('退出登录',onLogout,'danger')));
 return box;
}

export function topActions({me,isAdmin,translationConfig,onLogout,onRefresh}){
 return el('div',{class:'top-actions'},translationConfig.enabled?languageMenu(translationConfig):null,themeMenu(onRefresh),accountMenu(me,isAdmin,onLogout));
}
