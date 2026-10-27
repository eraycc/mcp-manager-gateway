import{el,api,button,field,selectField,dialog,formDialog,pretty,date,route,navigate,title,tablePage,jobDialog,directDownload}from './core.js';

const labels={
 'identity.login':'登录','identity.login_failed':'登录失败','identity.logout':'退出登录','identity.register':'注册账户','identity.session.revoke':'会话撤销',
 'token.create':'创建令牌','token.update':'修改令牌','token.delete':'删除令牌','token.rotate':'轮换令牌','token.secret.reveal':'查看令牌',
 'user.create':'创建用户','user.update':'修改用户','user.delete':'删除用户','user.role_change':'变更角色','user.batch':'批量管理用户',
 'mcp.create':'创建服务','mcp.update':'修改服务','mcp.delete':'删除服务','mcp.start':'启动服务','mcp.stop':'停止服务','mcp.refresh':'刷新目录',
 'mcp.proposal.submit':'提交提议','mcp.proposal.configure':'配置提议','mcp.proposal.approve':'批准提议','mcp.proposal.reject':'拒绝提议','mcp.proposal.delete':'删除提议',
 'settings.update':'修改系统设置','audit.delete':'清理审计记录','audit.clear':'清空审计记录','audit.clear_auto':'自动保留清理','audit.export':'导出审计记录','audit.rebuild':'重建审计索引',
 'logs.delete':'清理调用日志','logs.export':'导出调用日志','logs.rebuild':'重建日志索引'
};
const categories={identity:'身份认证',token:'访问令牌',user:'用户权限',mcp:'MCP 服务',settings:'系统设置',oauth:'OAuth',lease:'连接管理',logs:'调用日志',audit:'审计管理',storage:'存储治理',maintenance:'系统维护',update:'版本更新'};
const results={success:'成功',failure:'失败',pending:'进行中',unknown:'未知'};
const actors={user:'用户',system:'系统',anonymous:'未登录用户'};
const sources={web:'控制台',gateway:'网关',system:'系统',cli:'命令行',unknown:'历史记录'};
const fieldNames={title:'站点名称',role:'角色',disabled:'启用状态',scope_mode:'授权范围',mcp_ids:'授权服务',log_retention_days:'日志保留天数',enable_resource_tools:'资源工具',enable_mcp_proposal:'提议权限',exposeNativeResources:'原生资源列表'};
const size=value=>value<1024?value+' B':value<1048576?(value/1024).toFixed(1)+' KiB':(value/1048576).toFixed(1)+' MiB';
const cellText=(value,className='')=>el('span',{class:'audit-cell-text '+className,title:String(value??'—')},value??'—');
const outcome=value=>el('span',{class:'badge '+(value==='success'?'good':value==='failure'?'bad':'blue')},results[value]||value);
const localInput=value=>value?new Date(new Date(value).getTime()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,16):'';
function summary(row){
 let data;try{data=JSON.parse(row.summary)}catch{return row.summary||'—'}
 const fields=data.changed_fields||Object.keys(data.changes||{});
 if(fields.length)return'变更：'+fields.map(key=>fieldNames[key]||key).slice(0,5).join('、')+(fields.length>5?' 等':'');
 if(data.phase)return({started:'已开始',completed:'已完成',recovered:'恢复完成'}[data.phase]||data.phase)+(data.count!==undefined?' · '+data.count+' 条':'');
 if(data.count!==undefined)return'影响 '+data.count+' 条记录';
 return data.reason_code||data.error_type||'—';
}
function confirm(message,action){
 return new Promise(resolve=>{
  let success=false;
  const popup=formDialog('确认操作',[el('p',{},message)],async()=>{success=(await action())!==false},{saveLabel:'确认执行'});
  popup.addEventListener('close',()=>resolve(success),{once:true});
 });
}

export async function auditLogs(main,signal){
 main.classList.add('audit-page');
 const [initial,registered]=await Promise.all([api('/audit-logs/status',{signal}),api('/audit-logs/actions',{signal})]);
 const p=route().params,query=new URLSearchParams(p);query.delete('page');query.delete('page_size');
 const status=el('section',{class:'card audit-overview','aria-label':'审计日志概况'});
 const renderStatus=value=>{
  status.replaceChildren(
   el('div',{class:'audit-volume'},el('strong',{},value.events.toLocaleString()),el('span',{class:'muted'},'条记录'),
    el('span',{class:'muted'},'· '+value.files+' 个文件 · '+size(value.bytes))),
   el('div',{class:'audit-retention'},el('span',{},value.retention_days?'保留最近 '+value.retention_days+' 天':'无限保留'),
    button('保留策略',()=>navigate('settings'))));
  if(value.write_failures||value.index_failures||value.skipped_records||value.pending_deletions)
   status.append(el('p',{class:'notice',role:'status'},'日志状态需关注：写入失败 '+value.write_failures+' 次，索引失败 '+value.index_failures+' 次，跳过异常记录 '+value.skipped_records+' 条，待恢复清理 '+value.pending_deletions+' 项。可在更多操作中查看诊断。'));
 };
 renderStatus(initial);
 const updateStatus=async()=>renderStatus(await api('/audit-logs/status',{signal}));
 let reload=async()=>{},matching=0;
 const execute=async(path,options)=>{const result=await jobDialog(await api(path,options),{autoCloseOnSuccess:true});await updateStatus();await reload();return result?.status==='completed'};
 const filteredDelete=()=>confirm('永久删除当前筛选匹配的 '+matching+' 条审计记录？本次清理留痕及提交后新增的记录会保留。',()=>execute('/audit-logs/delete',{method:'POST',body:{ids:null,filters:Object.fromEntries(query),confirm:true}}));
 const more=el('details',{class:'action-menu audit-more'},el('summary',{},'更多操作'),el('div',{class:'action-menu-panel'},
  button('查看日志状态',async()=>dialog('审计日志状态',el('pre',{},pretty(await api('/audit-logs/status'))))),
  button('重建审计索引',()=>confirm('从审计原文重建查询索引？日志记录会保留。',()=>execute('/audit-logs/rebuild',{method:'POST'}))),
  button('清空审计记录',()=>confirm('永久清空当前全部审计记录？本次清空的操作留痕会保留。',()=>execute('/audit-logs?confirm=true',{method:'DELETE'})),'danger')));
 main.append(title('审计日志','追溯用户与系统对服务、令牌、权限和配置的关键操作。',[
  button('导出 JSON',()=>directDownload('/audit-logs/export?'+new URLSearchParams({...Object.fromEntries(query),format:'json'}),'audit-logs.json')),
  button('导出 CSV',()=>directDownload('/audit-logs/export?'+new URLSearchParams({...Object.fromEntries(query),format:'csv'}),'audit-logs.csv')),more]),status);
 const advanced=el('form',{class:'form-grid audit-filters'}),fields=[];
 for(const[key,label,type]of [['from_time','开始时间','datetime-local'],['to_time','结束时间','datetime-local'],['username','操作人','text'],['user_id','用户 ID','text'],['target_type','对象类型','text'],['target_id','对象 ID','text'],['ip','IP 地址','text']]){
  const f=field(label,type==='datetime-local'?localInput(p.get(key)):p.get(key)||'',type);f.input.setAttribute('aria-label',label);fields.push([key,f.input]);advanced.append(f.node);
 }
 for(const[key,label,options]of [
  ['action','具体动作',(registered.items||[]).map(item=>[item.action,(labels[item.action]||item.action)+' · '+item.action])],
  ['actor_type','操作者类型',Object.entries(actors)],['source','操作来源',Object.entries(sources)]]){
  const f=selectField(label,p.get(key)||'',[['','全部'],...options]);fields.push([key,f.input]);advanced.append(f.node);
 }
 advanced.append(el('div',{class:'actions'},el('button',{type:'submit',class:'primary'},'应用筛选'),button('重置筛选',()=>navigate('audit-logs'))));
 advanced.onsubmit=event=>{event.preventDefault();const next=new URLSearchParams(p);for(const[key,input]of fields){if(input.value)next.set(key,key.endsWith('_time')?new Date(input.value).toISOString():input.value);else next.delete(key)}next.set('page','1');navigate('audit-logs',next)};
 main.append(el('details',{class:'card audit-advanced'},el('summary',{},'高级筛选'),advanced));
 const showDetail=async row=>{
  const data=await api('/audit-logs/'+encodeURIComponent(row.event_id));
  const facts=el('dl',{class:'audit-detail-facts'});
  for(const[label,value]of [['时间',date(data.timestamp)],['操作人',data.username||actors[data.actor_type]||data.user_id],['用户 ID',data.user_id],['对象 ID',data.target_id],['动作',labels[data.action]||data.action],['结果',results[data.result]],['对象',data.target_name||data.target_id],['来源',sources[data.source]||data.source],['IP',data.ip],['事件 ID',data.event_id],['请求 ID',data.request_id]])
   facts.append(el('div',{},el('dt',{},label),el('dd',{},value||'—')));
  dialog('审计详情',el('div',{class:'stack'},facts,el('h3',{},'操作详情'),el('pre',{},pretty(data.details))));
 };
 const removeSelected=ids=>confirm('永久删除选中的 '+ids.length+' 条审计记录？本次操作会留痕。',async()=>{
  for(let start=0;start<ids.length;start+=500){if(!await execute('/audit-logs/delete',{method:'POST',body:{ids:ids.slice(start,start+500)}}))return false}return true;
 });
 reload=await tablePage(main,{path:'/audit-logs',signal,onData:data=>{matching=data.total},columns:[
  ['时间',row=>cellText(date(row.timestamp),'audit-time')],
  ['操作人',row=>el('div',{},cellText(row.username||actors[row.actor_type]||'未知'),cellText(sources[row.source]||row.source,'subtext'))],
  ['动作',row=>el('div',{},cellText(labels[row.action]||row.action),cellText(row.action,'subtext audit-action-code'))],
  ['对象',row=>el('div',{class:'audit-target'},cellText(row.target_name||row.target_type||'—'),cellText(row.target_id||'','subtext'))],
  ['结果',row=>outcome(row.result)],['IP',row=>cellText(row.ip)],['摘要',row=>{const text=summary(row);return el('span',{class:'audit-summary',title:text},text)}]
 ],filters:[['category','类别',Object.entries(categories)],['result','结果',Object.entries(results)]],
 actions:[button('删除全部匹配',filteredDelete,'danger')],rowActions:row=>[button('详情',()=>showDetail(row))],
 bulk:[['删除选中',removeSelected]]});
}
