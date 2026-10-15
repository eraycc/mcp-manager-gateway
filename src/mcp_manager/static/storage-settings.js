import{el,api,button,field,check,loading,watchJob}from './core.js';

const bytes=value=>{const n=Math.max(0,Number(value)||0);return n>=1024**3?(n/1024**3).toFixed(2)+' GiB':n>=1024**2?(n/1024**2).toFixed(2)+' MiB':n>=1024?(n/1024).toFixed(1)+' KiB':n+' B'};
const outcome=result=>({
 completed:'已释放 '+bytes(result.reclaimed_bytes)+'。'+(result.checkpoint_pending?'WAL 尚有活跃读取，待连接空闲后可再次释放。':'有效数据保持不变。'),
 busy:'数据库正忙，未完成压缩；请在调用较少时重试。',
 insufficient_space:'磁盘剩余空间不足，已跳过压缩；请先释放磁盘空间再重试。',
 below_threshold:'未达到回收阈值，无需压缩。',
 disabled:'启动自动回收已关闭。'
}[result?.status]||'任务未完成，请查看后台任务记录后重试。');

export function storageSettings(signal){
 const root=el('section',{class:'card stack'},el('h2',{},'存储治理'),
  el('p',{class:'notice'},'只回收数据库空闲页和碎片，不删除有效记录、目录缓存或向量索引。自动检查仅在启动时执行一次，不会每小时压缩。'),
  el('p',{class:'muted'},'两个阈值必须同时满足才自动执行 VACUUM；手动释放不受阈值限制。压缩期间数据库写入可能等待，建议在低峰期操作。WAL / SHM 由 SQLite 管理，请勿手动删除。'));
 const content=el('div',{class:'stack'},loading('正在读取数据库空间…'));root.append(content);
 const renderDatabase=(target,label,item)=>{
  if(!item.supported)return el('p',{class:'notice'},item.backend==='mysql'?'主数据库使用 MySQL，不提供 SQLite 空间回收配置；本地日志索引仍可独立治理。':'主数据库不是文件型 SQLite，不提供空间回收配置。');
  const group=el('fieldset',{class:'stack storage-database','aria-label':label},el('legend',{},label));
  const enabled=check('启动时按阈值自动释放空间',item.policy.startup_enabled);
  const mib=field('最小空闲空间（MiB）',item.policy.min_free_mib,'number','默认 32 MiB；这是可回收的空闲页，不是数据库总体积。');
  const percent=field('最小空闲比例（%）',item.policy.min_free_percent,'number','默认 20%；与最小空闲空间同时满足才自动压缩。');
  for(const [key,f]of [['mib',mib],['percent',percent]]){f.input.min=0;f.input.step='any';f.input.required=true;f.input.setAttribute('aria-label',f.node.querySelector('.field-label').textContent);const hint=f.node.querySelector('small');hint.id='storage-hint-'+target+'-'+key;f.input.setAttribute('aria-describedby',hint.id)}
  mib.input.max=1024**3;percent.input.max=100;
  const stats=el('p',{class:'muted'}),status=el('div',{class:'storage-status',role:'status','aria-live':'polite','aria-atomic':'true'}),error=el('p',{class:'error',role:'alert',hidden:true});
  const drawStats=value=>{const s=value.stats;stats.textContent=s?'数据库文件 '+bytes(s.file_bytes)+' · WAL '+bytes(s.wal_bytes)+' · 空闲 '+bytes(s.free_bytes)+'（'+s.free_percent.toFixed(2)+'%）':value.stats_error||'空间信息暂不可用';};
  drawStats(item);if(item.running)status.textContent='后台正在释放空间，稍后可刷新空间信息。';
  else if(item.last_result)status.textContent=outcome(item.last_result);
  const busy=value=>{group.setAttribute('aria-busy',String(value));for(const node of [enabled.input,mib.input,percent.input,save,compact,refresh])node.disabled=value};
  const waiting=message=>{const node=loading(message);node.removeAttribute('role');node.removeAttribute('aria-live');status.replaceChildren(node)};
  const perform=async(message,operation)=>{busy(true);error.hidden=true;waiting(message);try{await operation()}catch(e){if(e.name!=='AbortError'){status.replaceChildren();error.textContent=e.message;error.hidden=false}}finally{busy(false)}};
  const update=async()=>{const value=await api('/settings/storage',{signal});drawStats(value.databases[target]);return value.databases[target]};
  const save=button('保存回收设置',async()=>{
   if(!mib.input.reportValidity()||!percent.input.reportValidity())return;
   await perform('正在保存回收设置…',async()=>{
    const value=await api('/settings/storage',{method:'PATCH',body:{[target]:{startup_enabled:enabled.input.checked,min_free_mib:Number(mib.input.value),min_free_percent:Number(percent.input.value)}},signal});
    drawStats(value.databases[target]);status.textContent='回收设置已保存，下次启动生效。';
   });
  });
  const compact=button('释放数据库空间',()=>perform('正在提交空间回收任务…',async()=>{
   const job=await api('/settings/storage/'+target+'/vacuum',{method:'POST',signal});
   waiting('正在后台释放空间…');status.append(el('small',{},'切换页面不会中止压缩。任务编号：'+job.id));
   const done=await watchJob(job,signal);
   if(!['completed','completed_with_errors'].includes(done.status))throw new Error('任务未完成，请在低峰期重试。');
   const result=await api('/jobs/'+job.id+'/results/0',{signal});
   if(!result.ok)throw new Error(result.error||'空间释放失败，请稍后重试。');
   status.textContent=outcome(result.result);await update();
  }));
  const refresh=button('刷新空间信息',()=>perform('正在读取空间信息…',async()=>{const value=await update();status.textContent=value.running?'后台仍在释放空间，请稍后刷新。':value.last_result?outcome(value.last_result):'空间信息已刷新。'}));
  group.append(stats,enabled.node,el('div',{class:'form-grid settings-grid'},mib.node,percent.node),el('div',{class:'actions'},save,compact,refresh),status,error);
  return group;
 };
 const load=async()=>{content.replaceChildren(loading('正在读取数据库空间…'));try{
  const saved=await api('/settings/storage',{signal});
  content.replaceChildren(renderDatabase('main','主 SQLite',saved.databases.main),renderDatabase('logs','日志索引 SQLite',saved.databases.logs));
 }catch(e){if(e.name!=='AbortError')content.replaceChildren(el('p',{class:'error',role:'alert'},e.message),button('重试',load))}};
 load();return root;
}
