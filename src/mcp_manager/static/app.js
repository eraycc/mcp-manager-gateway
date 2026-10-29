import{$,el,icon,pretty,api,button,toast,run,field,selectField,check,jsonField,dialog,formDialog,detail,download,directDownload,badge,date,route,navigate,title,loading,empty,allPages,jobDialog,tablePage,resetTableSelection,capabilitySummary,cacheInfo,pagedList,tabPanels,enableTableDragging}from './core.js';
import{mcpEditor,proposalEditor,mcpTools,mcpImport,oauthDialog,credentialDialog,credentialStateLabel}from './mcps.js';
import{embeddingSettings}from './search-settings.js';
import{storageSettings}from './storage-settings.js';
import{auditLogs}from './audit-logs.js';
import{leases as connectionLeases}from './leases.js';
import{capabilityBrowser}from './capability-browser.js';
import{translationSettings}from './translation-settings.js';
import{DEFAULT_TRANSLATION_CONFIG,normalizeTranslationConfig,applyTranslation,disableTranslation,preferredTranslationTarget}from './translation.js';
import{initializeTheme,toggleTheme as theme,topActions}from './shell-controls.js';
import{cacheUpdateState,maybeNotifyUpdate,notificationDot,readCachedUpdateState,syncUpdateNotifications,updateAboutPanel,updateCheckSetting}from './update-ui.js';
let me,bootstrap,abort,refreshTimer,updateState,selectionPage,translationConfig=normalizeTranslationConfig(DEFAULT_TRANSLATION_CONFIG);
const nav=[['dashboard','仪表盘','grid'],['mcps','MCP 服务','server',true],['proposals','MCP 审批','approval',true],['tokens','访问令牌','key'],['leases','连接管理','link'],['logs','调用日志','logs'],['audit-logs','审计日志','audit',true],['users','用户管理','users',true],['profile','个人资料','user'],['settings','系统设置','settings',true]];
const admin=()=>me?.role==='admin';
const refresh=()=>render();
let updateRequest=0;
async function refreshUpdateState(path='/update-status',options={}){
 const request=++updateRequest,next=await api(path,options);
 if(request===updateRequest){updateState=next;cacheUpdateState(next);syncUpdateNotifications(next.notification_count,next)}
 return next;
}
function formAction(title,fields,action,options={}){
 return new Promise(resolve=>{
  let success=false;
  const popup=formDialog(title,fields,async()=>{success=(await action())!==false},options);
  popup.addEventListener('close',()=>resolve(success),{once:true})
 })
}
const ask=(message,action)=>formAction('确认操作',[el('p',{},message)],action,{saveLabel:'确认执行'});
async function batchResults(path,ids,values){
 const results=[];
 for(let offset=0;offset<ids.length;offset+=500){
  const batch=ids.slice(offset,offset+500),value=await api(path,{method:'POST',body:{...values,ids:batch}});
  results.push(...(value.results||[{ids:batch,...value}]))
 }
 detail('批量操作结果',results);return results.every(item=>item.ok!==false)
}
async function batchJobs(path,ids,values={},options={}){
 for(let offset=0;offset<ids.length;offset+=500){
  toast('提交批次 '+(Math.floor(offset/500)+1)+' / '+Math.ceil(ids.length/500));
  const result=await jobDialog(await api(path,{method:'POST',body:{...values,ids:ids.slice(offset,offset+500)}}),options);
  if(!result||!['completed','complete','done','success'].includes(result.status)){toast('操作未全部完成，请查看任务详情','fail');return false}
 }
 return true
}
initializeTheme();
let menuReturn=null,mobileNav='hidden';
const mobile=()=>matchMedia('(max-width:720px)').matches;
function syncNavigation(){
 const small=mobile(),collapsed=small?mobileNav==='rail':localStorage.getItem('mcp-sidebar')==='collapsed',expanded=small&&mobileNav==='expanded';
 document.body.dataset.mobileNav=mobileNav;
 document.body.classList.toggle('sidebar-collapsed',collapsed);document.body.classList.toggle('menu-open',expanded);
 const side=$('.sidebar'),shell=$('.shell'),toggle=$('.collapse-toggle'),trigger=$('.mobile-menu');
 if(side){side.inert=small&&mobileNav==='hidden';if(expanded){side.setAttribute('role','dialog');side.setAttribute('aria-modal','true')}else{side.removeAttribute('role');side.removeAttribute('aria-modal')}}
 if(shell)shell.inert=expanded;
 if(toggle){toggle.replaceChildren(icon(collapsed?'doubleRight':'doubleLeft'),el('span',{class:'collapse-text'},'收起'));toggle.setAttribute('aria-label',collapsed?'展开导航':'折叠导航');toggle.setAttribute('aria-expanded',String(!collapsed))}
 trigger?.setAttribute('aria-expanded',String(small&&mobileNav!=='hidden'));
}
function setDrawer(state){
 mobileNav=typeof state==='boolean'?(state?'expanded':'hidden'):state;
 if(mobileNav==='expanded')menuReturn=document.activeElement;
 syncNavigation();
 if(mobileNav==='expanded')$('.drawer-close')?.focus();else if(mobileNav==='hidden'){$('.mobile-menu')?.focus();menuReturn=null}else if(menuReturn?.isConnected){menuReturn.focus();menuReturn=null}
}
function shell(){
 const tooltip=el('div',{class:'nav-tooltip',role:'tooltip',id:'nav-tooltip',hidden:true});let tooltipFor=null;
 const hide=()=>{tooltip.hidden=true;tooltipFor=null};
 const show=(link,label)=>{if(!document.body.classList.contains('sidebar-collapsed'))return;tooltipFor=link;tooltip.textContent=label;const rect=link.getBoundingClientRect();tooltip.style.top=Math.min(rect.top,innerHeight-48)+'px';tooltip.style.left=(rect.right+12)+'px';tooltip.hidden=false};
 const bindTip=(link,label)=>{
  let revealTouch=false,timer;
  link.addEventListener('pointerenter',e=>{if(e.pointerType!=='touch')show(link,typeof label==='function'?label():label)});
  link.addEventListener('pointerleave',e=>{clearTimeout(timer);if(e.pointerType!=='touch')hide()});
  link.addEventListener('focus',()=>show(link,typeof label==='function'?label():label));link.addEventListener('blur',hide);
  link.addEventListener('pointerdown',e=>{if(e.pointerType==='touch'&&document.body.classList.contains('sidebar-collapsed')){revealTouch=tooltipFor!==link;show(link,typeof label==='function'?label():label);timer=setTimeout(()=>{revealTouch=true;show(link,typeof label==='function'?label():label)},450)}});
  link.addEventListener('pointerup',()=>clearTimeout(timer));link.addEventListener('pointercancel',()=>{clearTimeout(timer);revealTouch=false});
  link.addEventListener('click',e=>{if(revealTouch){e.preventDefault();e.stopImmediatePropagation();revealTouch=false;return}hide()},true)
 };
 const collapse=button('',()=>{hide();if(mobile())setDrawer(mobileNav==='rail'?'expanded':'rail');else{localStorage.setItem('mcp-sidebar',document.body.classList.contains('sidebar-collapsed')?'expanded':'collapsed');syncNavigation()}},'collapse-toggle');
 // Buttons act immediately on touch; long press titles are needed for navigation links.
 collapse.addEventListener('pointerenter',e=>{if(e.pointerType!=='touch')show(collapse,'展开')});collapse.addEventListener('pointerleave',hide);collapse.addEventListener('focus',()=>show(collapse,'展开'));collapse.addEventListener('blur',hide);
 const sidebar=el('aside',{class:'sidebar','aria-label':'侧边导航'},el('div',{class:'brand'},el('span',{class:'brand-icon'},icon('link')),el('span',{class:'brand-text'},bootstrap.title||'MCP Manager')),el('div',{class:'sidebar-controls'},button('关闭导航',()=>setDrawer('hidden'),'drawer-close'),collapse),el('div',{class:'nav-label'},'工作空间'));
 const navigation=el('nav',{'aria-label':'主导航',class:'sidebar-nav'});
 for(const[key,label,i]of nav.filter(x=>!x[3]||admin())){
  const count=key==='settings'?(updateState?.notification_count||0):0;const link=el('a',{href:'#/'+key,class:'nav-link '+(route().name===key?'active':''),'aria-label':label,'aria-current':route().name===key?'page':null,'aria-describedby':'nav-tooltip'},icon(i),el('span',{class:'nav-text'},label),count?notificationDot(count):null);bindTip(link,label);
  link.addEventListener('click',()=>{hide();if(mobile()&&mobileNav==='expanded')setDrawer('rail')});navigation.append(link)
 }
 sidebar.append(navigation,el('div',{class:'sidebar-foot'},el('div',{},'统一接入 · 按需运行'),el('div',{},admin()?'管理员工作空间':'个人工作空间')));
 const main=el('main',{id:'main',tabindex:'-1'}),menu=button('',()=>setDrawer(mobileNav==='hidden'?'rail':'hidden'),'mobile-menu');menu.append(icon('menu'));menu.setAttribute('aria-label','打开导航');
 const backdrop=button('关闭导航',()=>setDrawer('hidden'),'drawer-backdrop');backdrop.tabIndex=-1;
 const actions=topActions({me,isAdmin:admin(),translationConfig,onRefresh:refresh,onLogout:async()=>{await api('/auth/logout',{method:'POST'});me=null;await start()}});$('#app').replaceChildren(sidebar,backdrop,tooltip,el('div',{class:'shell'},el('header',{class:'topbar'},el('div',{class:'actions'},menu,el('span',{class:'muted'},'工作空间 / '+(nav.find(x=>x[0]===route().name)?.[1]||'仪表盘'))),actions),main));syncNavigation();return main}
async function activateSessionPlugins(){if(translationConfig.enabled){const applied=await applyTranslation(translationConfig,preferredTranslationTarget(translationConfig));applied.warnings.forEach(message=>toast(message,'warning'))}const next=await refreshUpdateState();if(updateState===next&&next.auto_check_enabled)maybeNotifyUpdate(next)}
async function openSession(){updateState=readCachedUpdateState();translationConfig=normalizeTranslationConfig(await api('/translation/settings'));await render();run(activateSessionPlugins)}
async function auth(register=false){resetTableSelection();selectionPage=null;const username=field('用户名'),password=field('密码','','password');username.input.required=true;username.input.autocomplete='username';password.input.required=true;password.input.autocomplete=register?'new-password':'current-password';if(register)password.input.minLength=10;const email=field('邮箱（可选）','','email');const err=el('div',{class:'error',hidden:true,role:'alert'}),submit=el('button',{type:'submit',class:'primary'},register?'创建账户':'登录');const form=el('form',{class:'stack'},username.node,password.node,register?email.node:null,err,submit);form.onsubmit=async e=>{e.preventDefault();submit.disabled=true;err.hidden=true;try{await api('/auth/'+(register?'register':'login'),{method:'POST',body:{username:username.input.value,password:password.input.value,...(register?{email:email.input.value||''}:{})}});if(register){toast('账户已创建，请登录');auth(false)}else{me=await api('/me');history.replaceState(null,'','#/dashboard');await openSession()}}catch(x){err.textContent=x.message;err.hidden=false}finally{submit.disabled=false}};
$('#app').replaceChildren(el('div',{class:'auth'},el('section',{class:'card'},el('div',{class:'brand'},el('span',{class:'brand-icon'},icon('link')),bootstrap.title||'MCP Manager'),el('h1',{},register?(!bootstrap.initialized?'初始化管理员':'创建账户'):'欢迎回来'),el('p',{class:'muted'},register?(!bootstrap.initialized?'第一个注册账户将成为系统管理员。':'创建账户后由管理员分配服务访问权限。'):'登录以管理服务、访问令牌和调用记录。'),form,el('div',{class:'actions',style:'margin-top:20px;justify-content:space-between'},bootstrap.registration_enabled||!bootstrap.initialized?button(register?'已有账户，登录':'创建账户',()=>auth(!register)):null,button('切换主题',theme)))))}
async function dashboard(main,signal){main.append(title('仪表盘','服务运行与工具调用，一目了然。',[button('刷新',refresh)]));const d=await api('/dashboard',{signal});const stats=[['总调用次数',d.calls,'累计调用'],['运行中服务',d.running_count,'共 '+d.mcp_count+' 个服务'],['访问令牌',d.token_count,'按权限访问工具'],['活跃租约',d.lease_count,'连接数 '+d.connection_count],['并发调用',d.runtime_capacity?.active_business_calls,'排队 '+(d.runtime_capacity?.queued_business_calls??0)]];main.append(el('div',{class:'stats'},stats.map(([a,b,c])=>el('div',{class:'stat'},el('span',{class:'muted'},a),el('strong',{},b??0),el('small',{},c)))));
const points=d.daily||[],max=Math.max(1,...points.map(x=>x.calls));const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 700 190');svg.setAttribute('class','chart');svg.setAttribute('role','img');svg.setAttribute('aria-label','每日调用量：'+points.map(x=>x.day+' '+x.calls+' 次').join('；'));for(let i=0;i<points.length;i++){const x=35+i*630/Math.max(1,points.length),h=points[i].calls/max*145;const rect=document.createElementNS(svg.namespaceURI,'rect');for(const[k,v]of Object.entries({x,y:155-h,width:Math.max(2,550/Math.max(1,points.length)),height:h,rx:3,fill:'#3b82f6'}))rect.setAttribute(k,v);const t=document.createElementNS(svg.namespaceURI,'title');t.textContent=points[i].day+': '+points[i].calls+' 次';rect.append(t);svg.append(rect)}
main.append(el('div',{class:'grid2'},el('section',{class:'card'},el('h2',{},'调用趋势'),points.length?svg:empty('暂无调用记录'),el('div',{class:'muted'},points.length?points[0].day+' — '+points.at(-1).day:'')),el('section',{class:'card'},el('h2',{},'调用结果'),...['success','failed','unknown','cancelled'].map((s,i)=>el('div',{class:'toolbar'},badge(['成功','失败','结果未知','已取消'][i]),el('strong',{},d[s]??d.by_status?.[s]??0))),el('p',{class:'muted',style:'margin-top:15px'},'停止服务 '+(d.stopped_count??0)+' 个'))));
main.append(el('section',{class:'card','data-dashboard-recent':''},el('h2',{},'最近调用'),d.recent?.length?el('div',{class:'table-scroll'},el('table',{},el('thead',{},el('tr',{},...['时间','服务 / 工具','结果','耗时'].map(x=>el('th',{},x)))),el('tbody',{},d.recent.map(x=>el('tr',{},el('td',{},date(x.timestamp)),el('td',{},x.mcp_name||x.mcp_id,' / ',x.tool_name),el('td',{},badge(x.status)),el('td',{},(x.duration_ms??'—')+' ms')))))):empty('首个工具调用完成后，将在这里显示。')));
 let busy=false;const tick=async()=>{if(busy||signal.aborted||document.hidden||!main.isConnected)return;busy=true;try{const next=await api('/dashboard',{signal});const values=[next.calls,next.running_count,next.token_count,next.lease_count,next.runtime_capacity?.active_business_calls];main.querySelectorAll('.stat strong').forEach((node,i)=>{const value=String(values[i]??0);if(node.textContent!==value)node.textContent=value});const subtitles=main.querySelectorAll('.stat small');if(subtitles[1])subtitles[1].textContent='共 '+(next.mcp_count??0)+' 个服务';if(subtitles[3])subtitles[3].textContent='连接数 '+(next.connection_count??0);if(subtitles[4])subtitles[4].textContent='排队 '+(next.runtime_capacity?.queued_business_calls??0);const stopped=main.querySelector('.grid2 .card:last-child>p');if(stopped)stopped.textContent='停止服务 '+(next.stopped_count??0)+' 个';const counts=main.querySelectorAll('.grid2 .toolbar strong');['success','failed','unknown','cancelled'].forEach((key,i)=>{if(counts[i])counts[i].textContent=next[key]??next.by_status?.[key]??0});if(pretty(next.daily)!==pretty(d.daily)){const list=next.daily||[],maximum=Math.max(1,...list.map(x=>x.calls));svg.replaceChildren();list.forEach((value,i)=>{const rect=document.createElementNS(svg.namespaceURI,'rect'),height=value.calls/maximum*145;for(const[k,v]of Object.entries({x:35+i*630/Math.max(1,list.length),y:155-height,width:Math.max(2,550/Math.max(1,list.length)),height,rx:3,fill:'#3b82f6'}))rect.setAttribute(k,v);const t=document.createElementNS(svg.namespaceURI,'title');t.textContent=value.day+': '+value.calls+' 次';rect.append(t);svg.append(rect)});const chartCard=main.querySelector('.grid2 .card');if(!svg.isConnected){chartCard.querySelector('.empty')?.remove();chartCard.insertBefore(svg,chartCard.children[1])}svg.setAttribute('aria-label','每日调用量：'+list.map(x=>x.day+' '+x.calls+' 次').join('；'))}const chartLabel=main.querySelector('.grid2 .card:first-child>.muted');if(chartLabel){const daily=next.daily||[];chartLabel.textContent=daily.length?daily[0].day+' — '+daily.at(-1).day:''}if(pretty(next.recent)!==pretty(d.recent)){const section=main.querySelector('[data-dashboard-recent]'),items=next.recent||[];section.querySelector('.empty')?.remove();let table=section.querySelector('table');if(items.length&&!table){table=el('table',{},el('thead',{},el('tr',{},...['时间','服务 / 工具','结果','耗时'].map(x=>el('th',{},x)))),el('tbody'));section.append(el('div',{class:'table-scroll'},table))}if(table){const tbody=table.querySelector('tbody');for(let i=0;i<items.length;i++){const item=items[i],values=[date(item.timestamp),(item.mcp_name||item.mcp_id||'')+' / '+item.tool_name,badge(item.status),(item.duration_ms??'—')+' ms'];let row=tbody.children[i];if(!row){row=el('tr',{},values.map(value=>el('td',{},value)));tbody.append(row)}else values.forEach((value,index)=>{const candidate=el('td',{},value);if(!row.children[index].isEqualNode(candidate))row.children[index].replaceChildren(...candidate.childNodes)})}while(tbody.children.length>items.length)tbody.lastChild.remove();table.parentElement.hidden=!items.length}if(!items.length)section.append(empty('首个工具调用完成后，将在这里显示。'))}Object.assign(d,next)}catch(e){if(e.name!=='AbortError')main.dataset.refreshError=e.message}finally{busy=false}};const timer=setInterval(tick,10000);signal.addEventListener('abort',()=>clearInterval(timer),{once:true})}

async function grants(value={}){
 const mode=selectField('服务范围',value.scope_mode||'selected',[['selected','仅指定服务'],['all','全部获授权服务（含未来新增）']]),selected=new Set(value.mcp_ids||[]);
 const search=field('搜索授权服务','','search'),protocol=selectField('授权协议筛选','',[['','全部协议'],...['stdio','streamable-http','sse','rest'].map(x=>[x,x])]),list=el('div',{class:'grant-list'}),count=el('div',{class:'grant-count muted','aria-live':'polite'}),pager=el('div',{class:'pagination'});
 let items=[],page=1,total=0,epoch=0,timer;
 const renderList=()=>{count.textContent='已选 '+selected.size+' 个 · 匹配 '+total+' 个';list.replaceChildren(...items.map(x=>{const c=check(x.name+' · '+x.transport,selected.has(x.id));c.input.addEventListener('change',()=>{c.input.checked?selected.add(x.id):selected.delete(x.id);renderList()});return c.node}));if(!items.length)list.append(el('span',{class:'muted'},'暂无匹配服务。'));pager.replaceChildren();const previous=button('上一页',()=>{page--;load()}),next=button('下一页',()=>{page++;load()});previous.disabled=page<=1;next.disabled=page*50>=total;pager.append(el('span',{class:'total'},'第 '+page+' / '+Math.max(1,Math.ceil(total/50))+' 页'),previous,next)};
 const load=async()=>{const request=++epoch,listQuery=new URLSearchParams({page:String(page),page_size:'50',q:search.input.value.trim()});if(protocol.input.value)listQuery.set('transport',protocol.input.value);list.replaceChildren(loading('正在加载服务摘要…'));const result=await api('/mcps?'+listQuery);if(request!==epoch)return;items=result.items||[];total=result.total||0;renderList()};
 const choosePage=()=>{mode.input.value='selected';items.forEach(x=>selected.add(x.id));toggle();renderList()};
 const controls=el('div',{class:'stack'},el('div',{class:'form-grid'},search.node,protocol.node),count,el('div',{class:'actions'},button('选择当前页',choosePage),button('清空选择',()=>{selected.clear();mode.input.value='selected';toggle();renderList()})),list,pager);
 const toggle=()=>controls.hidden=mode.input.value==='all';mode.input.onchange=toggle;search.input.oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>{page=1;load()},250)};protocol.input.onchange=()=>{page=1;load()};toggle();await load();
 return{node:el('div',{class:'stack grant-selector'},mode.node,controls),value:()=>({scope_mode:mode.input.value,mcp_ids:[...selected]})}
}
async function copyToken(row){
 let data;try{data=await api('/tokens/'+row.id+'/secret')}catch(e){if(e.code==='token_secret_unavailable'){await tokenEditor({...row,secret_available:false});return}throw e}
 try{await navigator.clipboard.writeText(data.token);toast('令牌已复制')}
 catch{secret(data.token)}
}
async function tokenEditor(row){
 const name=field('令牌名称',row?.name||'');name.input.required=true;
 const scope=await grants(row),discovery=selectField('工具发现模式',row?.discovery_mode||'discovery',[['discovery','按需发现工具'],['native','原生工具目录']]);
 const help=el('p',{class:'notice'});
 const describe=()=>help.textContent=discovery.input.value==='discovery'?
  '先发现 MCP 服务，再获取工具完整参数定义，最后按精确名称调用。系统设置中的混合检索配置对按需发现生效。':
  '客户端直接获取当前令牌授权范围内的全部工具定义。';
 discovery.input.onchange=describe;describe();
 const expiry=field('到期时间（可选）',row?.expires_at?new Date(new Date(row.expires_at).getTime()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,16):'','datetime-local');
 const resourceTools=check('启用资源工具',row?.enable_resource_tools),proposalTools=check('允许 Agent 提交/查询 MCP 提案',row?.enable_mcp_proposal),resourceHelp=el('div',{class:'stack'},resourceTools.node,el('p',{class:'muted'},'启用后可通过 gateway_search_resources / gateway_read_resource 按需发现与读取资源、提示词和模板。此选项独立于系统的原生资源列表开关。')),proposalHelp=el('div',{class:'stack'},proposalTools.node,el('p',{class:'muted'},'仅管理员 Token 可启用；新增 gateway_mcp_proposals，Agent 可单条或批量提交 MCP 配置进入审批队列，也可查询提案审核状态，不会直接创建服务。')),disabled=check('禁用令牌',row?.disabled),parts=[name.node,scope.node,discovery.node,help,resourceHelp,...(admin()?[proposalHelp]:[]),expiry.node];
 if(row){
  parts.push(disabled.node);let data;
  if(row.secret_available){try{data=await api('/tokens/'+row.id+'/secret')}catch(e){if(e.code!=='token_secret_unavailable')throw e}}
  if(data){
   const value=field('访问令牌值',data.token);
   value.input.readOnly=true;value.input.setAttribute('aria-label','访问令牌值');value.input.autocomplete='off';
   parts.push(value.node,button('复制令牌',()=>copyToken(row)));
  }else parts.push(el('p',{class:'notice'},'此令牌的原值未保存或暂时无法解密，仍可编辑其设置。轮换一次后即可回显和复制新值。'));
 }
 formDialog(row?'编辑访问令牌':'创建访问令牌',parts,async()=>{
  const result=await api('/tokens'+(row?'/'+row.id:''),{method:row?'PATCH':'POST',body:{
   name:name.input.value,...scope.value(),discovery_mode:discovery.input.value,enable_resource_tools:resourceTools.input.checked,...(admin()?{enable_mcp_proposal:proposalTools.input.checked}:{}),
   expires_at:expiry.input.value?new Date(expiry.input.value).toISOString():null,...(row?{disabled:disabled.input.checked}:{})}});
  await refresh();if(result.token)secret(result.token);
 });
}
function secret(value){
 const box=el('code',{class:'secret'},value);
 dialog('访问令牌',el('div',{class:'stack'},el('p',{class:'notice'},'可在访问令牌列表中再次复制，或打开编辑查看完整值。'),
  box,button('复制令牌',async()=>{await navigator.clipboard.writeText(value);toast('已复制')})));
}
async function tokens(main,signal){
 main.append(title('访问令牌','为不同客户端创建独立凭据和服务范围。',[button('创建令牌',()=>tokenEditor(),'primary')]));
 if(admin()){
  const params=route().params,view=selectField('令牌视图',params.get('view')||'mine',[['mine','我的令牌'],['all','全部用户令牌']]);
  const username=field('按用户名筛选',params.get('username')||'');username.input.setAttribute('aria-label','按用户名筛选');
  username.input.disabled=view.input.value!=='all';
  const filters=el('form',{class:'toolbar'},view.node,username.node,el('label',{class:'filter-submit-field'},el('span',{class:'field-label'},' '),el('button',{type:'submit'},'筛选用户')));
  const apply=()=>{const p=new URLSearchParams(params);p.set('view',view.input.value);p.set('page','1');
   if(view.input.value==='all'&&username.input.value.trim())p.set('username',username.input.value.trim());else p.delete('username');
   if(view.input.value==='mine')p.delete('user_id');navigate('tokens',p)};
  view.input.onchange=apply;filters.onsubmit=e=>{e.preventDefault();apply()};main.append(filters);
 }
 await tablePage(main,{path:'/tokens',signal,columns:[
  ['名称',x=>el('div',{},x.name,el('span',{class:'subtext code'},x.prefix+'…'))],
  ...(admin()?[['所属用户',x=>x.username||x.user_id]]:[]),
  ['工具目录',x=>el('div',{},badge(x.discovery_mode),el('span',{class:'subtext'},'网关工具 '+(3+(x.enable_resource_tools?2:0)+(x.enable_mcp_proposal?1:0))+' 个'))],['范围',x=>x.scope_mode==='all'?'全部授权服务':(x.mcp_ids?.length||0)+' 个服务'],
  ['状态',x=>badge(x.disabled?'disabled':'active')],
  ['调用次数',x=>el('div',{},x.call_count??'—',el('span',{class:'subtext'},'成功 '+(x.success_count??'—')+' / 失败 '+(x.failed_count??'—')))],
  ['到期',x=>date(x.expires_at)]],
  filters:[['disabled','状态',[['false','启用'],['true','禁用']]]],
  rowActions:r=>[...(r.secret_available?[button('复制',()=>copyToken(r))]:[]),button('统计',async()=>detail('令牌调用统计',await api('/tokens/'+r.id+'/stats'))),
   button('编辑',()=>tokenEditor(r)),button('轮换',()=>ask('轮换后旧令牌立即失效。',async()=>{
    const d=await api('/tokens/'+r.id+'/rotate',{method:'POST'});secret(d.token);refresh()})),
   button('删除',()=>ask('删除令牌“'+r.name+'”？',async()=>{await api('/tokens/'+r.id,{method:'DELETE'});refresh()}),'danger')],
  bulk:[...['enable','disable','delete'].map((a,i)=>[['批量启用','批量禁用','批量删除'][i],ids=>ask('对 '+ids.length+' 个令牌执行此操作？',async()=>{
   const results=[];for(const id of ids){try{
    await api('/tokens/'+id,{method:a==='delete'?'DELETE':'PATCH',...(a==='delete'?{}:{body:{disabled:a==='disable'}})});results.push({id,ok:true})
   }catch(e){results.push({id,ok:false,error:e.message})}}
   detail('批量操作结果',results);return results.every(item=>item.ok)})])]
 });
}
async function users(main,signal){main.append(title('用户管理','管理账户、角色和 MCP 服务授权。',[button('创建用户',()=>userEditor(),'primary')]));await tablePage(main,{path:'/users',signal,columns:[['用户',x=>el('div',{},x.username,el('span',{class:'subtext'},x.email||'未设置邮箱'))],['角色',x=>badge(x.role)],['服务范围',x=>x.scope_mode==='all'?'全部服务':(x.mcp_ids?.length||0)+' 个服务'],['状态',x=>badge(x.disabled?'disabled':'active')],['创建时间',x=>date(x.created_at)]],filters:[['role','角色',[['admin','管理员'],['user','普通用户']]],['disabled','状态',[['false','正常'],['true','禁用']]]],rowActions:r=>[button('编辑',()=>userEditor(r)),button('删除',()=>ask('删除用户“'+r.username+'”及其访问权限？',async()=>{await api('/users/'+r.id,{method:'DELETE'});refresh()}),'danger')],bulk:[...['enable','disable','delete'].map((a,i)=>[['批量启用','批量禁用','批量删除'][i],ids=>ask('对 '+ids.length+' 个用户执行'+['启用','禁用','删除'][i]+'？',()=>batchResults('/users/batch',ids,{action:a}))])]})}
async function userEditor(row){const username=field('用户名',row?.username||''),email=field('邮箱',row?.email||'','email'),password=field(row?'新密码（留空不修改）':'密码','','password'),role=selectField('角色',row?.role||'user',[['user','普通用户'],['admin','管理员']]),disabled=check('禁用账户',row?.disabled),scope=await grants(row);username.input.required=true;password.input.required=!row;password.input.minLength=10;formDialog(row?'编辑用户':'创建用户',[el('div',{class:'form-grid'},username.node,email.node,password.node,role.node),scope.node,disabled.node],async()=>{await api('/users'+(row?'/'+row.id:''),{method:row?'PATCH':'POST',body:{username:username.input.value,email:email.input.value||'',role:role.input.value,disabled:disabled.input.checked,...scope.value(),...(password.input.value?{password:password.input.value}:{})}});refresh()})}
function serviceCapabilities(row){
 const summary=row.personal_credentials;
 if(!summary)return capabilitySummary(row)||el('span',{class:'muted'},'暂无能力缓存');
 return el('div',{class:'service-capabilities'},
  el('span',{class:'subtext'},summary.current_user_configured?'当前用户能力列表':'当前用户未认证'),
  summary.current_user_configured||row.cache_stale?capabilitySummary(row,{includeUnavailable:summary.current_user_configured}):null,
  el('span',{class:'subtext',title:'统计有权访问此服务的启用用户（包含管理员）；凭据须完整保存，OAuth 还需有效授权。'},'已认证 '+summary.configured_users+' / '+summary.total_users))
}
async function mcps(main,signal){main.append(title('MCP 服务','集中配置服务，按需启动，共享 MCP 完整能力。',[button('导入配置',()=>mcpImport(refresh)),button('导出配置',()=>ask('是否导出全部 MCP 服务配置为 JSON 文件？敏感字段将脱敏，重新导入后需补齐。',async()=>download('mcp-services.json',await api('/mcps/export',{method:'POST',body:{include_secrets:false}})))),button('新增服务',()=>mcpEditor(null,refresh),'primary')]));await tablePage(main,{path:'/mcps',signal,columns:[['服务名称',x=>el('div',{},el('strong',{},x.name),el('span',{class:'subtext'},x.description||x.slug),x.startup_failure_count>0?el('span',{class:x.status==='failed'?'subtext danger':'subtext'},'连续失败 '+x.startup_failure_count+' 次 · '+(x.last_startup_error||'原因未知')):null)],['传输',x=>badge(x.transport)],['运行策略',x=>badge(x.mode)],['运行状态',x=>badge(x.status||'stopped')],['能力列表',serviceCapabilities],['缓存状态 / 时间',x=>cacheInfo(x)]],filters:[['transport','传输',[['stdio','stdio'],['streamable-http','Streamable HTTP'],['sse','SSE'],['rest','REST']]],['mode','策略',[['lazy','按需'],['eager','常驻'],['disabled','禁用']]],['status','状态',[['running','运行中'],['ready','就绪'],['stopped','已停止'],['failed','连续失败']]]],rowActions:r=>[button('能力/测试',()=>mcpTools(r,admin())),button('编辑',()=>mcpEditor(r,refresh)),button('更多',()=>mcpMore(r))],bulk:[...['start','stop','refresh','lazy','enable','disable','delete'].map((a,i)=>[['启动','停止','刷新缓存','设为按需','设为常驻','设为禁用','删除'][i],ids=>ask('对 '+ids.length+' 个服务执行此操作？',async()=>{const success=await batchJobs('/mcps/batch',ids,{action:a},{autoCloseOnSuccess:a==='delete',title:a==='delete'?'正在删除服务':'后台任务'});if(success&&a==='delete')toast('删除完成','success');return success})]),['导出',async ids=>download('mcp-services.json',await api('/mcps/export',{method:'POST',body:{ids,include_secrets:false}})),{reload:false}]]})}
function mcpMore(r){const d=dialog(r.name+' · 服务操作',el('div',{class:'stack'},...['start','stop','refresh','copy'].map((a,i)=>button(['启动服务','停止服务','刷新能力缓存','复制服务'][i],async()=>{try{const data=await api('/mcps/'+r.id+'/'+a,{method:'POST'});detail('操作结果',data)}finally{refresh()}})),r.auth_type==='oauth'?button('OAuth 授权',()=>oauthDialog(r,admin(),refresh)):null,button('删除服务',()=>ask('删除服务“'+r.name+'”？',async()=>{await api('/mcps/'+r.id,{method:'DELETE'});d.close();refresh()}),'danger')))}

async function rejectProposal(row){const reason=field('拒绝原因','','textarea');reason.input.required=true;formDialog('拒绝 MCP 提议',[reason.node],async()=>{await api('/mcp-proposals/'+row.id+'/reject',{method:'POST',body:{reason:reason.input.value}});refresh()},{saveLabel:'确认拒绝'})}
function proposalTestResult(row){
 const result=row.test_result||{},tested=row.tested_at?el('span',{class:'subtext'},'最近测试：'+date(row.tested_at)):null;
 if(row.test_status==='success'){
  const errors=new Map((result.capability_errors||[]).map(x=>[x.capability,x.error])),part=(key,label,count)=>errors.has(key)?label+'不可用（'+errors.get(key)+'）':label+' '+(count??0)+(key==='tools'?' 个':'');
  return el('div',{},errors.size?'部分可用':'成功',el('span',{class:'subtext'},[['tools','工具',result.tool_count],['resources','资源',result.resource_count],['prompts','提示词',result.prompt_count],['templates','模板',result.template_count]].map(x=>part(...x)).join(' · ')),tested)
 }
 if(row.test_status==='failed')return el('div',{},el('span',{class:'danger'},'测试失败'),el('span',{class:'subtext danger'},row.test_error||'原因未知'),tested);
 if(row.test_status==='cancelled')return el('div',{},'已取消',tested);
 return el('div',{},'待测试（pending）',tested)
}
function proposalTestButton(row){
 const resource='/mcp-proposals/'+row.id+'/test-capabilities';
 const showCached=()=>capabilityBrowser({resource,title:row.name+' · 测试能力'});
 const test=async()=>{
  toast('正在测试 MCP 连接…','info');
  try{
   const result=await api('/mcp-proposals/'+row.id+'/test?summary=true',{method:'POST'});
   toast('测试成功：发现 '+(result.tool_count||0)+' 个工具','success');
   showCached();
  }catch(e){toast('测试失败：'+e.message,'fail')}
  finally{refresh()}
 };
 return button('能力测试',async()=>{
  if(row.auth_type==='oauth'){toast('OAuth 类型需要在审批通过并授权后验证','warning');return}
  if(row.test_status==='success'){
   const choice=dialog('已有能力目录',el('div',{class:'stack'},
    el('p',{class:'notice'},'检测到已缓存的能力目录，可直接查看，或重新连接服务刷新能力目录。'),
    el('div',{class:'actions'},
     button('查看缓存能力目录',()=>{choice.close();showCached()}),
     button('刷新能力目录',async()=>{choice.close();await test()},'primary'))));
   return;
  }
  await test();
 });
}

async function proposals(main,signal){
 main.append(title('MCP 审批','审核 Agent 提交的标准 MCP 配置，配置策略并测试后加入服务列表。'));
 await tablePage(main,{path:'/mcp-proposals',signal,columns:[
  ['MCP',x=>el('div',{},el('strong',{},x.name),el('span',{class:'subtext'},x.purpose||'未声明用途'))],
  ['来源',x=>el('div',{},x.source||'agent',el('span',{class:'subtext code'},x.token_id))],
  ['协议',x=>badge(x.transport)],['声明能力',x=>(x.declared_capabilities||[]).join('、')||'—'],
  ['请求权限',x=>(x.requested_permissions||[]).join('、')||'—'],['状态',x=>badge(x.status)],
  ['测试结果',proposalTestResult],['策略 / 隔离',x=>(x.mode||'待配置')+' / '+(x.isolation||'待配置')],['提交时间',x=>date(x.created_at)]],
  filters:[['status','状态',[['pending','待审批'],['incomplete','待完善配置'],['approved','已审批'],['rejected','已拒绝']]]],
  rowActions:r=>[
   button('详情',async()=>detail('提议详情',await api('/mcp-proposals/'+r.id))),
   ...(!['approved','rejected'].includes(r.status)&&r.test_status==='success'?[button('能力目录',()=>capabilityBrowser({resource:'/mcp-proposals/'+r.id+'/test-capabilities',title:r.name+' · 测试能力'}))]:[]),
   ...(!['approved','rejected'].includes(r.status)?[
    button('配置',()=>proposalEditor(r,refresh)),
    proposalTestButton(r),
    button('通过',()=>ask('确认通过并添加到 MCP 服务列表？'+(r.mode&&r.isolation?'':'（未配置启动策略/实例隔离，将使用默认值：按需/服务共享。也可稍后在 MCP 服务列表内配置）'),async()=>{await api('/mcp-proposals/'+r.id+'/approve',{method:'POST',body:{}});refresh()}),'primary'),
    button('拒绝',()=>rejectProposal(r),'danger')
   ]:[]),
   button('删除',()=>ask('从审批列表删除“'+r.name+'”？',async()=>{await api('/mcp-proposals/'+r.id,{method:'DELETE'});refresh()}),'danger')],
  bulk:[
   ['批量配置',ids=>{const mode=selectField('启动策略','lazy',[['lazy','按需'],['eager','常驻'],['disabled','禁用']]),isolation=selectField('实例隔离','service',[['service','服务共享'],['user','用户隔离'],['session','会话隔离']]),ci=selectField('OAuth 配置隔离','',[['','未设置'],['shared','共享配置'],['user','独享配置']]);return formAction('批量配置',[mode.node,isolation.node,ci.node],()=>batchResults('/mcp-proposals/batch',ids,{action:'configure',configuration:{mode:mode.input.value,isolation:isolation.input.value,config_isolation:ci.input.value||null}}))}],
   ['批量测试',ids=>batchJobs('/mcp-proposals/batch-test',ids,{}, {title:'MCP 批量测试',stopLabel:'停止测试',completeLabel:'关闭'})],
   ['批量通过',ids=>ask('通过 '+ids.length+' 条提议并添加服务？（未配置启动策略/实例隔离的将使用默认值：按需/服务共享。也可稍后在 MCP 服务列表内配置）',()=>batchResults('/mcp-proposals/batch',ids,{action:'approve'}))],
   ['批量拒绝',ids=>{const reason=field('统一拒绝原因','','textarea');reason.input.required=true;return formAction('批量拒绝',[reason.node],()=>batchResults('/mcp-proposals/batch',ids,{action:'reject',reason:reason.input.value}))}],
   ['批量删除',ids=>ask('删除 '+ids.length+' 条审批记录？',()=>batchResults('/mcp-proposals/batch',ids,{action:'delete'}))]
  ]
 })
}

async function logs(main,signal){main.append(title('调用日志','追踪请求来源、工具参数、执行结果与耗时。',admin()?[button('重建日志索引',()=>ask('从日志原文重建索引？',async()=>jobDialog(await api('/logs/rebuild',{method:'POST'}))))]:[]));const p=route().params;const advanced=el('form',{class:'card form-grid'}),entries=[...(admin()?[['username','用户名'],['user_id','用户 ID']]:[]),['token_id','令牌 ID'],['mcp_id','服务 ID'],['tool_name','工具名称'],['from_time','开始时间'],['to_time','结束时间'],['source','来源']].map(([k,l])=>{const f=field(l,p.get(k)||'',k.endsWith('_time')?'datetime-local':'text');advanced.append(f.node);return[k,f.input]});advanced.append(el('button',{type:'submit'},'应用筛选'));advanced.onsubmit=e=>{e.preventDefault();const q=new URLSearchParams(p);for(const[k,f]of entries){if(f.value)q.set(k,k.endsWith('_time')?new Date(f.value).toISOString():f.value);else q.delete(k)}q.set('page','1');navigate('logs',q)};const disclosure=el('details',{},el('summary',{},'高级筛选'),advanced);main.append(disclosure);
await tablePage(main,{path:'/logs',signal,columns:[['时间',x=>date(x.timestamp)],['用户 / 令牌',x=>el('div',{},x.username||'匿名',el('span',{class:'subtext'},x.token_name||'—'))],['服务 / 工具',x=>el('div',{},x.mcp_name||x.mcp_id,el('span',{class:'subtext code'},x.tool_name))],['结果',x=>badge(x.status)],['耗时',x=>(x.duration_ms??'—')+' ms'],['来源','source']],filters:[['status','结果',[['success','成功'],['tool_error','工具失败'],['gateway_error','网关失败'],['outcome_unknown','结果未知'],['cancelled','已取消']]]],actions:[button('导出筛选结果',()=>directDownload('/logs/export?'+p,'mcp-logs.json')),...(admin()?[button('删除全部匹配',()=>ask('永久删除所有匹配日志？',async()=>{await jobDialog(await api('/logs/delete',{method:'POST',body:{ids:null,filters:Object.fromEntries(p)}}));refresh()}),'danger')]:[])],rowActions:r=>[button('详情',()=>logDetail(r))],bulk:admin()?[['删除选中',ids=>ask('永久删除 '+ids.length+' 条日志？',()=>batchJobs('/logs/delete',ids,{filters:{}}))]]:null})}
async function logDetail(row){
 const panel=el('div',{class:'stack'},loading('正在加载有限预览…')),popup=dialog('调用详情',panel);
 try{
  const result=await api('/logs/'+row.id+'/preview');
  if(!popup.isConnected)return;
  const children=[el('pre',{},result.preview),el('div',{class:'actions'},button('下载完整日志',()=>directDownload('/logs/'+row.id+'/download','mcp-log.json')))];
  if(result.truncated)children.unshift(el('p',{class:'notice'},'该日志较大，当前仅显示前 '+result.preview.length+' 个字符；完整内容请直接下载。'));
  panel.replaceChildren(...children);
 }catch(error){if(popup.isConnected)panel.replaceChildren(el('div',{class:'error',role:'alert'},error.message))}
}
async function profile(main,signal){main.append(title('个人资料','管理账户、登录会话与客户端连接。'));const username=field('用户名',me.username),email=field('邮箱',me.email||'','email'),old=field('当前密码','','password'),password=field('新密码（可选）','','password');username.input.required=true;password.input.minLength=10;const form=el('form',{class:'card stack'},el('h2',{},'账户信息'),el('div',{class:'form-grid'},username.node,email.node,old.node,password.node),el('button',{type:'submit',class:'primary'},'保存资料'));form.onsubmit=e=>{e.preventDefault();run(async()=>{me=await api('/me',{method:'PATCH',body:{username:username.input.value,email:email.input.value||'',...(password.input.value?{current_password:old.input.value,password:password.input.value}:{})}});toast('资料已保存');refresh()})};main.append(form);main.append(connectionGuide());
const sessions=await api('/me/sessions',{signal}),sessionItems=Array.isArray(sessions)?sessions:sessions.items||[],otherSessions=sessionItems.filter(s=>!s.current),activeOthers=otherSessions.filter(s=>s.active);const revokeAll=button('撤销全部',()=>ask('撤销当前会话之外的全部登录会话？其他设备需要重新登录。',async()=>{const result=await api('/me/sessions/revoke-others',{method:'POST'});toast('已撤销 '+result.count+' 个会话');refresh()})),deleteAll=button('删除全部',()=>ask('永久删除当前会话之外的全部登录会话记录？其他设备会立即退出，此操作不可恢复。',async()=>{const result=await api('/me/sessions/delete-others',{method:'POST'});toast('已永久删除 '+result.count+' 个会话');refresh()}),'danger');revokeAll.disabled=!activeOthers.length;deleteAll.disabled=!otherSessions.length;main.append(el('section',{class:'card'},el('div',{class:'toolbar'},el('h2',{},'登录会话'),el('div',{class:'actions'},revokeAll,deleteAll)),el('p',{class:'muted'},'撤销会话会立即使该登录失效；永久删除还会移除会话记录。批量操作不会影响当前会话。'),pagedList({items:sessionItems,key:'sessions',label:'会话',searchText:s=>[s.user_agent,s.id,s.ip_address,date(s.created_at)].join(' '),filters:[['current','当前会话',s=>s.current],['active','有效会话',s=>s.active],['revoked','已撤销',s=>s.revoked]],renderItem:s=>el('div',{class:'toolbar session-row'},el('div',{class:'session-details'},el('div',{class:'actions'},s.current?badge('当前会话'):null,badge(s.revoked?'已撤销':s.active?'有效':'已失效')),el('div',{},'登录 IP：'+(s.ip_address||'未记录')),el('div',{class:'session-agent'},'User-Agent：'+(s.user_agent||'未记录')),el('div',{class:'muted'},'登录时间：'+date(s.created_at))),s.current?null:el('div',{class:'actions session-actions'},s.active?button('撤销会话',()=>ask('撤销此登录会话？该设备需要重新登录。',async()=>{await api('/me/sessions/'+s.id,{method:'DELETE'});toast('会话已撤销');refresh()})):null,button('永久删除',()=>ask('永久删除此登录会话及其记录？该设备会立即退出，此操作不可恢复。',async()=>{await api('/me/sessions/'+s.id+'/permanent',{method:'DELETE'});toast('会话已永久删除');refresh()}),'danger')))})));
main.append(await credentialServices(signal))
}
function credentialServiceActions(service,state){
 const personal=state.required&&state.isolation==='user',actions=[];
 if(personal&&(['bearer','basic','api_key'].includes(state.auth_type)||state.auth_type==='oauth'&&state.config_isolation==='user')){
  actions.push(button(state.auth_type==='oauth'?'OAuth 配置':'配置凭据',()=>credentialDialog(service,refresh)))
 }
 if(personal&&state.auth_type==='oauth'&&state.action!=='configure_oauth')actions.push(button('授权',()=>oauthDialog(service,false,refresh,'profile')));
 return actions
}
async function credentialServices(signal){
 const body=el('div',{class:'list-items'}),foot=el('div',{class:'pagination'}),search=el('input',{type:'search',placeholder:'搜索可访问服务…','aria-label':'搜索服务'}),form=el('form',{class:'toolbar'},search,el('button',{type:'submit'},'搜索'));
 const section=el('section',{class:'card','aria-label':'服务列表'},el('h2',{},'可访问的服务'),form,body,foot);
 let page=1,total=0,epoch=0;
 const load=async()=>{const request=++epoch,query=new URLSearchParams({page:String(page),page_size:'50',q:search.value.trim(),enabled_only:'true'});body.replaceChildren(loading('正在加载服务与凭据状态…'));foot.replaceChildren();try{const result=await api('/mcps?'+query,{signal}),services=result.items||[];const statuses=services.length?await api('/mcps/credentials/status-batch',{method:'POST',body:{ids:services.map(item=>item.id)},signal}):{items:[]};if(request!==epoch||signal?.aborted)return;total=result.total||0;const byId=new Map(statuses.items.map(item=>[item.id,item]));body.replaceChildren(...services.map(service=>{const state=byId.get(service.id)||{};return el('div',{class:'toolbar credential-service'},el('div',{class:'credential-service-info'},el('div',{class:'credential-service-name'},el('strong',{},service.name),state.required?badge(credentialStateLabel(state.state)):null,service.mode==='disabled'?badge('disabled'):null),service.description?el('p',{class:'credential-service-description'},service.description):null,capabilitySummary(service)),el('div',{class:'actions'},badge(service.transport),button('查看能力',()=>mcpTools(service,false)),credentialServiceActions(service,state)))}));if(!services.length)body.append(empty('暂无匹配的服务；尚未获授权时请联系管理员。'));const previous=button('上一页',()=>{page--;load()}),next=button('下一页',()=>{page++;load()});previous.disabled=page<=1;next.disabled=page*50>=total;foot.append(el('span',{class:'total'},'共 '+total+' 个服务'),previous,next)}catch(error){if(error.name!=='AbortError'&&request===epoch)body.replaceChildren(el('div',{class:'error',role:'alert'},error.message))}};
 form.addEventListener('submit',event=>{event.preventDefault();page=1;load()});await load();return section
}
function connectionGuide(){
const endpoint=location.origin+'/mcp',command='mcp-manager stdio --url '+endpoint;
const copyBlock=(label,text)=>el('div',{class:'stack'},el('h3',{},label),el('pre',{},text),button('复制'+label,async()=>{await navigator.clipboard.writeText(text);toast('已复制')}));
const http='POST '+endpoint+'\nAuthorization: Bearer <你的令牌>\nMCP-Protocol-Version: 2026-07-28\nContent-Type: application/json\nAccept: application/json, text/event-stream\n\n{"jsonrpc":"2.0","id":"discover-1","method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"my-client","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}';
const toml='[mcp_servers.manager]\nurl = "'+endpoint+'"\nbearer_token_env_var = "MCP_MANAGER_TOKEN"';
const json=pretty({mcpServers:{manager:{command:'mcp-manager',args:['stdio','--url',endpoint],env:{MCP_MANAGER_TOKEN:'<你的令牌>'}}}});
const stdio=['mmg','mcp-manager','mcp-manager-gateway'].map(name=>name+' stdio --url '+endpoint).join('\n')+'\n\n# 或显式传入令牌（任一入口均支持）\n'+command+' --token "<你的令牌>"';
return el('section',{class:'card'},el('h2',{},'连接 MCP 客户端'),el('p',{class:'notice'},'先启动 MCP Manager 网关并保持运行，再连接客户端。请在“访问令牌”中创建令牌，并替换示例占位符。'),
tabPanels([['http','HTTP 接入',el('div',{class:'stack'},el('p',{},'支持 Streamable HTTP 的客户端可直接连接此端点。现代 SDK 会自动使用 server/discover，并在连接旧服务时回退 initialize 握手。'),copyBlock('HTTP 请求示例',http))],
['codex','Codex / 客户端配置',el('div',{class:'stack'},el('p',{},'Codex 使用 config.toml。先在运行 Codex 的环境中设置 MCP_MANAGER_TOKEN；以下配置通过环境变量读取令牌。'),copyBlock('Codex TOML',toml),el('p',{},'采用 mcpServers JSON 配置的客户端可使用本地 stdio 桥接。将下面示例合并到客户端配置中。'),copyBlock('客户端 JSON',json))],
['stdio','本地 stdio',el('div',{class:'stack'},el('p',{},'使用 uv tool 安装 MCP Manager 后，可在任意目录运行以下命令。mmg、mcp-manager 与 mcp-manager-gateway 是等效入口，任选其一。可设置环境变量 MCP_MANAGER_TOKEN，也可使用 --token 参数。'),copyBlock('环境变量示例','PowerShell:\n$env:MCP_MANAGER_TOKEN = "<你的令牌>"\n\nLinux / macOS:\nexport MCP_MANAGER_TOKEN="<你的令牌>"'),copyBlock('启动命令',stdio))]],{key:'connect_tab',label:'客户端接入方式'}),
el('div',{class:'notice',style:'margin-top:20px'},'工具目录读取缓存，不会仅因浏览目录而启动服务。新增或修改工具后，请联系管理员刷新目录缓存。默认空闲 24 小时回收服务，可由管理员调整；stdio 桥接退出会释放该客户端连接，共享服务仍可供其他客户端使用。'))}
function aboutPanel(about,autoCheckNode){return updateAboutPanel({about,state:updateState,autoCheckNode,onCheck:async()=>{const next=await refreshUpdateState('/update-status/check',{method:'POST'});if(next.check_error)toast('更新检测失败：'+next.check_error,'warning');else toast(next.update_available?'发现新版本 '+next.latest_version:'当前已经是最新版本',next.update_available?'warning':'success')},onIgnore:async()=>{const next=await refreshUpdateState('/update-status/ignore',{method:'POST'});toast('已忽略版本 '+next.ignored_version,'info')}})}
async function settings(main,signal){
 main.append(title('系统设置','配置账户策略、匿名访问和运行维护。'));
 const original=await api('/settings',{signal});
 const fields={},form=el('form',{class:'stack settings-form'}),settingPanels=[],translation=translationSettings(original.translation_config),updateCheck=updateCheckSetting(original.update_auto_check,original.update_check_interval_seconds);
 const hints={cors_origins:'每行填写一个来源，例如 https://app.example.com。默认 * 允许所有来源的外部网页使用访问令牌接入 MCP；填写具体来源可限制接入范围。仅适用于 MCP 调用和连接续期，管理控制台仍只允许同源访问。',allowed_hosts:'每行一个 Host、IP 或 Host:端口。* 和 0.0.0.0 均允许全部监听地址；默认 *。仅校验 MCP 与网关 API，控制面板继续使用现有同源规则。',idle_seconds:'管理 MCP 服务实例：实例无任何调用达到此时长后自动停止，仅对 lazy 生效，不看租期引用数；0 表示不因空闲超时回收。',connection_ttl_seconds:'管理连接/租期/会话：无心跳或 MCP 调用达到此时长后自动释放租期，包括 bridge；默认 43200 秒（12 小时），0 表示不自动释放到期租期；Bridge 失联检测由下方判定次数独立控制。两项独立计时，eager 实例继续常驻。',bridge_heartbeat_seconds:'Bridge 是将 MMG 以 stdio 模式接入客户端的桥接程序。心跳间隔必须为大于 0 的整数（1～86400 秒），默认 90 秒；用于续期并同步配置，禁用失联检测后仍按此间隔发送心跳。已有 Bridge 在下次心跳后采用新间隔。',bridge_missed_heartbeats:'默认 3 次：连续缺失 3 个心跳间隔后判定失联并断开 Bridge（90 秒间隔约 270 秒）。0 表示禁用 Bridge 失联检测，仍正常发送心跳，连接有效期独立生效。关闭检测立即生效；重新启用后，已有连接在下次心跳后采用新次数。',startup_failure_threshold:'调用或能力目录刷新共用连续失败计数；达到阈值才标记 failed 并置空目录。任一调用或完整目录刷新成功后清零，未达阈值保留上次成功目录并显示失败原因。',log_retention_days:'仅控制调用日志。默认 0 为无限保留；设为 7 时自动清理超过 7 天的调用记录，清空后的日文件自动删除。',audit_log_retention_days:'仅控制审计日志。默认 0 为无限保留；设为 7 时自动清理超过 7 天的审计记录，清理操作保留审计留痕。'};
 const defs=[['基本设置',[['title','站点名称','text'],['registration_enabled','允许注册','check'],['allowed_hosts','允许的监听 Host / IP 列表','hosts'],['jwt_days','登录有效期（天，0 为永不过期）','number'],['cors_origins','允许跨域接入的来源','origins']]],['运行与日志',[['idle_seconds','默认空闲回收时间（秒）','number'],['connection_ttl_seconds','默认连接有效期（秒）','number'],['bridge_heartbeat_seconds','Bridge 默认心跳间隔（秒）','number'],['bridge_missed_heartbeats','Bridge 失联判定次数','number'],['startup_failure_threshold','连续失败阈值','number'],['log_retention_days','调用日志保留天数（0 为无限）','number'],['audit_log_retention_days','审计日志保留天数（0 为无限）','number'],['refresh_enabled','定时刷新工具缓存','check'],['refresh_cron','刷新 Cron 表达式','text'],['timezone','时区','text']]]];
 for(const [heading,list]of defs){
  const card=el('section',{class:'card'},el('h2',{},heading)),grid=el('div',{class:'form-grid settings-grid'});
  for(const[k,label,type]of list){
   const f=type==='check'?check(label,original[k]):field(label,['origins','hosts'].includes(type)?(original[k]??['*']).join('\n'):original[k],['origins','hosts'].includes(type)?'textarea':type,hints[k]||'');
   fields[k]=[f.input,type];
   if(['idle_seconds','connection_ttl_seconds','bridge_heartbeat_seconds','bridge_missed_heartbeats','startup_failure_threshold'].includes(k))f.node.classList.add('span2');
   if(hints[k]){f.input.setAttribute('aria-label',label);const hint=f.node.querySelector('small');hint.id='settings-hint-'+k;f.input.setAttribute('aria-describedby',hint.id)}
   if(k==='title')f.node.classList.add('site-title-field');
   if(k==='jwt_days')f.node.classList.add('settings-duration');
   if(type==='number'){f.input.min=['startup_failure_threshold','bridge_heartbeat_seconds'].includes(k)?1:0;f.input.required=true;if(['startup_failure_threshold','bridge_missed_heartbeats'].includes(k))f.input.max=100;if(k==='bridge_heartbeat_seconds')f.input.max=86400}
   if(!['cors_origins','allowed_hosts'].includes(k))grid.append(f.node);
  }
  if(heading==='基本设置'){
   card.classList.add('settings-basic');
   const titleGroup=el('div',{class:'settings-row'},grid.querySelector('.site-title-field'));
   const corsGroup=el('div',{class:'settings-row'},fields.cors_origins[0].parentElement),hostsGroup=el('div',{class:'settings-row'},fields.allowed_hosts[0].parentElement);
   card.append(titleGroup,grid,hostsGroup,corsGroup);
  }else card.append(grid);
  if(heading==='运行与日志')card.append(button('预览未来执行时间',async()=>detail('定时计划',await api('/settings/cron-preview',{method:'POST',body:{expression:fields.refresh_cron[0].value,timezone:fields.timezone[0].value}}))));
  settingPanels.push([heading==='基本设置'?'basic':'runtime',heading,card]);
 }
 const allowAnonymous=check('允许匿名访问',!original.token_auth_enabled),allowAnonymousHint=el('small',{id:'settings-hint-token_auth_enabled'},'开启后无令牌请求可访问范围内授权服务，已配置令牌的客户端不受影响，默认关闭');
 allowAnonymous.input.setAttribute('aria-describedby',allowAnonymousHint.id);fields.token_auth_enabled=[allowAnonymous.input,'inverse-check'];
 const scope=await grants({scope_mode:original.anonymous_scope_mode,mcp_ids:original.anonymous_mcp_ids}),scopePrompt=el('p',{class:'muted','aria-live':'polite'},'请先开启');
 const syncAnonymousScope=()=>{const enabled=allowAnonymous.input.checked;scope.node.querySelectorAll('input,select,textarea,button').forEach(node=>node.disabled=!enabled);scope.node.style.opacity=enabled?'':'0.55';scope.node.setAttribute('aria-disabled',String(!enabled));scopePrompt.hidden=enabled};
 allowAnonymous.input.addEventListener('change',syncAnonymousScope);syncAnonymousScope();
 settingPanels.push(['anonymous','匿名访问',el('section',{class:'card'},el('h2',{},'匿名访问'),el('div',{class:'stack'},allowAnonymous.node,allowAnonymousHint),el('h3',{},'匿名访问范围'),el('p',{class:'notice'},'此范围仅在允许匿名访问开启时生效'),scopePrompt,scope.node)]);
 settingPanels.push(['translation','翻译设置',translation.node]);
 const about=el('div',{},el('h2',{},'关于'),loading());
 const loadAbout=async()=>{try{const content=aboutPanel(await api('/about',{signal}),updateCheck.node);about.replaceChildren(content);run(()=>refreshUpdateState())}catch(e){if(e.name==='AbortError')return;about.replaceChildren(el('h2',{},'关于'),el('p',{class:'error',role:'alert'},e.message),button('重试',loadAbout))}};
 settingPanels.push(['storage','存储治理',storageSettings(signal)]);
 settingPanels.push(['embedding','工具检索',await embeddingSettings(signal)]);
 const aboutLabel=el('span',{class:'tab-label'},'关于',updateState?.notification_count?notificationDot(updateState.notification_count):null);settingPanels.push(['about',aboutLabel,about]);
 form.append(tabPanels(settingPanels,{key:'settings_tab',label:'系统设置分类'}));
 const err=el('div',{class:'error',hidden:true}),submit=el('button',{type:'submit',class:'primary'},'保存设置');
 form.append(err,el('div',{class:'actions settings-save'},submit));
 form.onsubmit=async e=>{e.preventDefault();submit.disabled=true;err.hidden=true;try{const payload={};for(const[k,[input,type]]of Object.entries(fields))payload[k]=type==='check'?input.checked:type==='inverse-check'?!input.checked:type==='number'?Number(input.value):['origins','hosts'].includes(type)?input.value.split(/[\n,]+/).map(x=>x.trim()).filter(Boolean):input.value;const s=scope.value();payload.anonymous_scope_mode=s.scope_mode;payload.anonymous_mcp_ids=s.mcp_ids;payload.translation_config=translation.value();payload.update_auto_check=updateCheck.value();payload.update_check_interval_seconds=updateCheck.intervalValue();await api('/settings',{method:'PATCH',body:payload});bootstrap.title=payload.title;const previousTranslation=translationConfig;translationConfig=normalizeTranslationConfig(payload.translation_config);if(!translationConfig.enabled&&previousTranslation.enabled)await disableTranslation(previousTranslation);await refresh();if(translationConfig.enabled){const applied=await applyTranslation(translationConfig,preferredTranslationTarget(translationConfig));applied.warnings.forEach(message=>toast(message,'warning'))}toast('设置已保存')}catch(x){err.textContent=x.message;err.hidden=false}finally{submit.disabled=false}};
 main.append(form);await loadAbout();
}
async function render(){abort?.abort();clearTimeout(refreshTimer);abort=new AbortController();if(!me)return;const current=route().name;if(selectionPage!==current){resetTableSelection();selectionPage=current}if(nav.find(x=>x[0]===current)?.[3]&&!admin()){navigate('dashboard');return}const main=shell();const spinner=loading();main.append(spinner);try{const pages={dashboard,mcps,proposals,tokens,leases:(node,signal)=>connectionLeases(node,signal,me),logs,'audit-logs':auditLogs,users,profile,settings};await(pages[current]||dashboard)(main,abort.signal);spinner.remove();}catch(e){spinner.remove();if(e.name==='AbortError')return;if(e.status===401){me=null;auth();return}main.append(el('div',{class:'card stack'},el('div',{class:'error',role:'alert'},e.message),button('重试',refresh)))}}
async function start(){try{bootstrap=await api('/bootstrap');document.title=bootstrap.title||'MCP Manager';try{me=await api('/me')}catch(e){if(e.status!==401)throw e}if(me){await openSession()}else auth(!bootstrap.initialized)}catch(e){$('#app').replaceChildren(el('div',{class:'auth'},el('section',{class:'card stack'},el('h1',{},'暂时无法连接'),el('div',{class:'error'},e.message),button('重试',start))))}}
window.addEventListener('hashchange',()=>{document.querySelectorAll('dialog').forEach(d=>d.close());run(render)});document.addEventListener('keydown',e=>{if(document.body.classList.contains('menu-open')){if(e.key==='Escape'){e.preventDefault();setDrawer(false)}if(e.key==='Tab'){const nodes=[...$('.sidebar').querySelectorAll('a,button')].filter(n=>n.getClientRects().length);const first=nodes[0],last=nodes.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}}});enableTableDragging();start();
window.addEventListener('resize',syncNavigation);
document.addEventListener('session-expired',()=>{abort?.abort();me=null;document.querySelectorAll('dialog').forEach(d=>d.close());auth();toast('登录已过期，请重新登录。','warning')});
