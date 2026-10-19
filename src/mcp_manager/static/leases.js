import{el,api,button,toast,field,check,formDialog,detail,badge,date,route,navigate,title,tablePage}from './core.js';

const outcomeLabels={stopped:'已停止',draining:'等待调用排空',shared:'仍有共享引用',kept_alive:'常驻保留'};
const statusLabels={active:'调用中',idle:'空闲',expiring:'即将到期',expired:'已到期'};
function confirm(message,action){
 return new Promise(resolve=>{
  let done=false;
  const popup=formDialog('确认操作',[el('p',{class:'danger'},message)],async()=>{await action();done=true},{saveLabel:'确认执行'});
  popup.addEventListener('close',()=>resolve(done),{once:true})
 })
}
function resultView(results){return results.map(r=>({...r,instances:(r.instances||[]).map(i=>({...i,outcome:outcomeLabels[i.outcome]||i.outcome}))}))}
function optionsFor(root,label,values,initialValue=''){
 const input=root.querySelector('select[aria-label="'+label+'"]');if(!input)return;
 const value=input.dataset.options===undefined?(initialValue||input.value):input.value,options=[['','全部'+label],...values];
 if(value&&!options.some(x=>x[0]===value))options.push([value,value]);
 const signature=JSON.stringify(options);if(input.dataset.options===signature)return;
 input.replaceChildren(...options.map(([id,name])=>el('option',{value:id},name)));input.value=value;input.dataset.options=signature
}
const localTime=value=>{if(!value)return '';const d=new Date(value);if(Number.isNaN(d.getTime()))return '';const pad=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes())};

function instanceCell(instances=[]){
 const many=instances.length>3;
 return el('div',{class:'lease-instance-cell'},
  el('div',{class:'lease-instances',tabindex:many?'0':null,role:many?'region':null,'aria-label':many?'引用 MCP 实例，可上下滚动':null},
   instances.length?instances.map(i=>el('div',{class:'lease-instance-entry'},el('span',{class:'lease-instance-name',title:i.name||i.server_id},i.name||i.server_id),el('span',{class:'subtext'},i.isolation+' · '+i.phase+' · '+i.lease_count+' 个共享引用 · '+i.in_flight+' 个实例调用'))):el('span',{class:'muted'},'无绑定 / 未启动')),
  instances.length>=3?el('span',{class:'lease-instance-hint muted'},'共 '+instances.length+' 个实例'+(many?' · 上下滚动查看':'')):null)
}
export async function leases(main,signal,actor){
 const isAdmin=actor.role==='admin',params=route().params;
 let reload=async()=>{},latestUsers=[],busy=false;
 const paused=check('暂停自动刷新',false),error=el('p',{class:'error',role:'status',hidden:true});
 main.append(title('连接管理','查看并管理活跃的 MCP 调用租期',[
  button('刷新',()=>reload(),'primary'),paused.node
 ]),el('p',{class:'muted'},'断开会释放租期并关闭关联会话。无引用的按需实例会回收；常驻实例可能保留。已发送调用可能继续完成，客户端之后仍可重新连接。'),error);
 const advanced=el('form',{class:'card form-grid'});
 const times=[['touched_from','心跳开始时间'],['touched_to','心跳结束时间']].map(([key,label])=>{
  const f=field(label,localTime(params.get(key)),'datetime-local');advanced.append(f.node);return[key,f.input]
 });
 advanced.append(el('button',{type:'submit'},'应用时间筛选'));
 advanced.onsubmit=e=>{e.preventDefault();const q=new URLSearchParams(params);for(const[k,input]of times){if(input.value)q.set(k,new Date(input.value).toISOString());else q.delete(k)}q.set('page','1');navigate('leases',q)};
 main.append(el('details',{},el('summary',{},'心跳时间范围'),advanced));
 const disconnect=async row=>{
  const other=row.user_id!==actor.id;
  const message=(other?'这是 '+row.username+' 的租期（非你本人）。':'')+'断开租期 '+row.id+'？引用归零的按需实例会回收；正在执行的调用可能继续完成。';
  return confirm(message,async()=>{
   busy=true;
   try{
    const result=await api('/leases/'+encodeURIComponent(row.id)+'/disconnect',{method:'POST'});
    detail('断开结果',resultView([result]));toast(result.ok?'租期已释放，请查看实例结果':'部分操作未完成，请查看结果',result.ok?'success':'warning');
    await reload()
   }finally{busy=false}
  })
 };
 const batch=ids=>confirm('断开选中的 '+ids.length+' 条租期？'+(isAdmin?'可能包含他人的租期，请核对用户筛选。当前可见用户：'+latestUsers.map(x=>x.username).join('、')+'。':'')+'正在执行的调用可能继续完成。',async()=>{
  busy=true;
  try{
   const results=[];
   for(let offset=0;offset<ids.length;offset+=200){
    const batch=ids.slice(offset,offset+200);
    try{
     const data=await api('/leases/batch',{method:'POST',body:{lease_ids:batch}});
     results.push(...data.results)
    }catch{
     results.push(...batch.map(lease_id=>({lease_id,ok:false,error:'outcome_unknown',message:'请求未确认完成，请刷新核对实际状态；本次不会重试。'})));
     results.push(...ids.slice(offset+200).map(lease_id=>({lease_id,ok:false,error:'not_submitted'})));
     break
    }
   }
   detail('断开结果',resultView(results))
  }finally{busy=false}
 });
 const table=await tablePage(main,{path:'/leases',signal,pollInterval:5000,
  pollEnabled:()=>!paused.input.checked&&!busy,pruneSelection:true,
  onPollError:e=>{error.hidden=!e;error.textContent=e?'刷新失败，当前显示上次快照：'+e.message:''},
  onData:data=>{
   latestUsers=data.user_refs||[];
   optionsFor(main,'MCP',(data.mcp_refs||[]).map(x=>[x.mcp_id,x.mcp_name]),params.get('mcp_id')||'');
   if(isAdmin)optionsFor(main,'用户',latestUsers.map(x=>[x.user_id,x.username]),params.get('user_id')||'')
  },
  columns:[
   ['租期 ID',r=>el('div',{},el('span',{class:'code',title:r.id},r.id.slice(0,8)),button('复制 ID',async()=>{await navigator.clipboard.writeText(r.id);toast('已复制 ID')}))],
   ['调用方',r=>el('div',{},r.username,el('span',{class:'subtext'},r.token_name||'无令牌名称'),isAdmin&&r.user_id!==actor.id?el('span',{class:'subtext danger'},'他人租期'):null)],
   ['引用 MCP / 实例',r=>instanceCell(r.instances)],
   ['类型',r=>badge(r.kind)],
   ['状态',r=>el('div',{class:'lease-activity',title:'当前执行数是瞬时值。累计仅统计本租期派发的下游业务调用（含失败尝试），目录搜索与维护刷新不计入；租期释放或网关重启后重置。'},el('div',{class:'lease-activity-summary'},badge(statusLabels[r.status]||r.status),el('span',{class:'subtext'},'累计下游调用 '+(r.call_count||0)+' 次')),el('span',{class:'subtext'},'当前执行 '+(r.in_flight||0)+' · 等待派发 '+(r.queued_calls||0)),el('span',{class:'subtext'},r.last_call_at?'最近调用 '+date(r.last_call_at):'尚无下游调用'))],
   ['心跳时间',r=>el('div',{},date(r.touched_at),el('span',{class:'subtext'},Math.floor(r.idle_seconds||0)+' 秒前'))],
   ['心跳结束时间',r=>el('div',{},r.expires_in==null?'不过期':date(r.expires_at),r.expires_in!=null?el('span',{class:'subtext'},r.expired?'已到期':Math.ceil(r.expires_in)+' 秒后'):null,r.kind==='bridge'&&r.bridge_disconnect_at?el('span',{class:'subtext'},'Bridge 失联判定：'+date(r.bridge_disconnect_at)+' · 已缺失 '+r.bridge_missed_heartbeats+' 次'):null)],
   ['创建时间',r=>date(r.created_at)]
  ],
  filters:[...(isAdmin?[['user_id','用户',[]]]:[]),['mcp_id','MCP',[]],
   ['kind','类型',[['agent','agent'],['bridge','bridge'],['retention','retention']]],
   ['status','状态',Object.entries(statusLabels)],
   ['sort','排序',[['touched_desc','最近活跃'],['created_desc','最近创建'],['ttl_asc','最快到期'],['in_flight_desc','当前执行最多']]]],
  rowActions:r=>[button('详情',async()=>detail('租期详情',await api('/leases/'+encodeURIComponent(r.id),{signal}))),button('断开',()=>disconnect(r),'danger')],
  bulk:[['批量断开',batch]]
 });
 reload=table;
}
