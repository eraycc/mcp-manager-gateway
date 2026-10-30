import{el,api,button,field,loading,watchJob}from './core.js';

const MIB=1024**2;
const bytes=n=>{n=Math.max(0,Number(n)||0);return n>=1024**3?(n/1024**3).toFixed(2)+' GiB':n>=MIB?(n/MIB).toFixed(2)+' MiB':n>=1024?(n/1024).toFixed(1)+' KiB':n+' B'};
const fields=[
 ['cache_cas_bytes','缓存正文容量（MiB）',MIB,'达到上限后暂停新缓存写入，保留当前可用目录。'],
 ['durable_cas_bytes','持久结果容量（MiB）',MIB,'0 为不额外限制；有效结果仅按保留策略或手动操作删除。'],
 ['catalog_sql_bytes','目录索引容量（MiB）',MIB,'限制新增目录索引；当前版本继续可用。'],
 ['retiring_sql_bytes','待回收索引容量（MiB）',MIB,'达到预算时先回收旧索引，暂停新一轮重建。'],
 ['staging_bytes','临时写入总容量（MiB）',MIB,'所有 worker 共享，按实际写入量预约。'],
 ['staging_slots','临时写入并发数',1,'所有 worker 共享的写入槽位。'],
 ['durable_reserve_bytes','持久写入预留容量（MiB）',MIB,'包含在临时总预算内，缓存不能占用。'],
 ['durable_reserve_slots','持久写入预留槽位',1,'包含在临时总并发内，缓存不能占用。'],
 ['min_free_bytes','磁盘最小剩余空间（MiB）',MIB,'与剩余比例同时保护；不足时暂停新正文写入。'],
 ['min_free_percent','磁盘最小剩余比例（%）',1,'在写入前检查，避免填满磁盘。'],
 ['wal_warn_bytes','WAL 提醒阈值（MiB）',MIB,'提示长事务或维护压力。'],
 ['wal_stop_bytes','WAL 暂停重建阈值（MiB）',MIB,'暂停新增可重建数据，等待正常检查点完成。'],
 ['query_cache_bytes','查询缓存容量（MiB）',MIB,'查询缓存可重建，不影响身份、配置或持久结果。'],
 ['query_cache_rows','查询缓存条数',1,'限制每类查询缓存的条数。'],
 ['query_cache_ttl_seconds','查询缓存有效期（秒）',1,'向量查询缓存超期后可自动清理。'],
 ['snapshot_memory_bytes','目录内存预算（MiB）',MIB,'0 为关闭内存缓存；查询仍受实时授权检查。'],
 ['artifact_memory_bytes','正文内存预算（MiB）',MIB,'0 为关闭正文内存缓存。'],
 ['gc_cycle_milliseconds','每轮后台清理时间（毫秒）',1,'小批清理并主动让出执行时间。'],
 ['gc_row_budget','每轮清理行数上限',1,'限定单轮工作量，避免清理长时间占用资源。'],
 ['gc_interval_seconds','空闲清理间隔（秒）',1,'有待回收数据时会更频繁地分批处理。'],
 ['legacy_call_tombstones','旧调用删除防护条数',1,'仅旧版调用 ID 使用；达到上限时先重启全部 worker 安全回收旧回调，或明确提高预算。'],
];
const domains={
 main:'主数据库',indexes:'派生索引',cache_objects:'缓存正文',durable_objects:'持久正文',
 calls:'调用日志',audit:'审计日志',call_payloads:'调用详情',call_deletions:'日志删除记录',
 call_sources_metadata:'日志源信息',manifests:'目录版本',temporary:'临时文件',
 backups:'迁移备份',legacy_objects:'旧版正文',legacy_cache:'旧版缓存',legacy_jobs:'旧版任务',
 configuration:'配置',other_logs:'其他日志',other:'其他受管文件',
};

export function storageGovernance(signal){
 const root=el('fieldset',{class:'stack storage-governance','aria-label':'容量与清理策略'},el('legend',{},'容量与清理策略'));
 const content=el('div',{class:'stack'},loading('正在读取存储治理信息…'));root.append(content);
 const load=async()=>{try{
  const saved=await api('/settings/storage/governance',{signal});
  const retention=el('p',{class:'notice'},'调用日志：'+(saved.retention.calls.permanent?'永久保存（0）':saved.retention.calls.days+' 天')+'；审计日志：'+(saved.retention.audit.permanent?'永久保存（0）':saved.retention.audit.days+' 天')+'。保留天数可在系统设置中调整，超过保留期自动删除。');
  const status=el('p',{role:'status','aria-live':'polite'}),error=el('p',{class:'error',role:'alert',hidden:true});
  const info=el('div',{class:'stack'}),draw=value=>{
   info.replaceChildren(el('p',{class:'muted'},value.files.complete?'文件统计已完成。':'文件统计正在分批扫描，以下为当前进度。'),
    el('p',{},'临时写入 '+value.staging.slots+' 个 · '+bytes(value.staging.bytes)+'；目录内存 '+bytes(value.memory.directory_bytes)+'；正文内存 '+bytes(value.memory.artifact_bytes)));
   const table=el('table',{},el('thead',{},el('tr',{},el('th',{},'存储域'),el('th',{},'文件'),el('th',{},'占用'))));
   const body=el('tbody',{});for(const[key,item]of Object.entries(value.files.domains))body.append(el('tr',{},el('td',{},domains[key]||'其他受管文件'),el('td',{},String(item.files)),el('td',{},bytes(item.bytes))));table.append(body);info.append(el('div',{class:'table-scroll'},table));
  };draw(saved);
  const controls=new Map(),grid=el('div',{class:'form-grid settings-grid'});
  for(const[key,label,unit,hint]of fields){const f=field(label,saved.policy[key]/unit,'number',hint);f.input.min=0;f.input.step=unit===1?'1':'any';f.input.required=true;f.input.setAttribute('aria-label',label);controls.set(key,[f.input,unit]);grid.append(f.node)}
  const details=el('details',{},el('summary',{},'调整容量、内存与后台清理预算'),grid);
  const buttons=[];
  const perform=async(task)=>{root.setAttribute('aria-busy','true');error.hidden=true;for(const b of buttons)b.disabled=true;for(const[input]of controls.values())input.disabled=true;try{await task()}catch(e){if(e.name!=='AbortError'){error.textContent=e.message;error.hidden=false;status.textContent='操作未完成。'}}finally{root.setAttribute('aria-busy','false');for(const b of buttons)b.disabled=false;for(const[input]of controls.values())input.disabled=false}};
  const save=button('保存治理策略',()=>{for(const[input]of controls.values())if(!input.reportValidity())return;return perform(async()=>{
   const body={};for(const[key,[input,unit]]of controls)body[key]=key==='min_free_percent'?Number(input.value):Math.round(Number(input.value)*unit);
   const value=await api('/settings/storage/governance',{method:'PATCH',body,signal});draw(value);status.textContent='治理策略已保存并生效。';
  })});
  const collect=button('回收已失效数据',()=>perform(async()=>{
   status.textContent='正在后台分批回收…';
   const job=await api('/settings/storage/governance/collect',{method:'POST',signal});const done=await watchJob(job,signal);
   if(done.status!=='completed')throw new Error('回收任务未完成，请查看任务记录。');
   const result=await api('/jobs/'+job.id+'/results/0',{signal});if(!result.ok)throw new Error(result.error||'回收失败。');
   status.textContent=result.result.status==='another_worker'?'其他 worker 正在执行回收。':'本轮已回收 '+result.result.deleted+' 条失效记录；仍有剩余时后台继续处理。';
   draw(await api('/settings/storage/governance',{signal}));
  }));
  const clear=button('清除查询缓存',()=>{if(!confirm('清除可重建的查询缓存？当前目录与持久结果会保留。'))return;return perform(async()=>{
   const job=await api('/settings/storage/governance/query-cache/clear',{method:'POST',signal});const done=await watchJob(job,signal);
   if(done.status!=='completed')throw new Error('清理未完成，请查看任务记录。');
   status.textContent='查询缓存已清理；无引用正文将由后台回收。';draw(await api('/settings/storage/governance',{signal}));
  })});
  const backupPanel=el('div',{class:'stack'});
  const backup=(await api('/settings/storage/governance/backup',{signal})).backup;
  if(backup?.exists){const remove=button('删除迁移备份',()=>{
   if(!confirm('永久删除迁移备份 '+backup.id+'？请先完成升级验收并另存所需恢复副本。'))return;
   return perform(async()=>{const job=await api('/settings/storage/governance/backup/'+backup.id+'/delete',{method:'POST',body:{confirm:backup.id},signal});const done=await watchJob(job,signal);
    if(done.status!=='completed')throw new Error('备份清理未完成，请查看任务记录。');
    backupPanel.replaceChildren(el('p',{class:'muted'},'该迁移备份已删除。'));status.textContent='迁移备份已删除。';draw(await api('/settings/storage/governance',{signal}));
   });
  });remove.disabled=!backup.ready;buttons.push(remove);backupPanel.append(el('p',{class:'notice'},backup.ready?'迁移备份保留用于恢复；验收并另存必要副本后，可单独删除。':'迁移尚未完成，备份受保护。'),remove)}
  const refresh=button('刷新治理信息',()=>perform(async()=>{draw(await api('/settings/storage/governance',{signal}));status.textContent='治理信息已刷新。'}));
  buttons.push(save,collect,clear,refresh);
  content.replaceChildren(retention,info,details,el('div',{class:'actions'},save,collect,clear,refresh),backupPanel,
   el('p',{class:'muted'},'回收操作处理已过期或无引用的数据。永久日志可在调用日志或审计日志页面按条件明确删除。'),status,error);
 }catch(e){if(e.name!=='AbortError')content.replaceChildren(el('p',{class:'error',role:'alert'},e.message),button('重试',load))}};
 load();return root;
}
