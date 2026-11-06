import{api,el,field,selectField,check,date}from './core.js';

export async function outboundSettings(signal){
 let current=await api('/settings/outbound-proxy',{signal});
 const enabled=check('启用出站代理',current.enabled);
 const mode=selectField('代理模式',current.mode==='env'?'env':'manual',[['manual','手动配置'],['env','跟随环境变量（高级）']]);
 const scheme=selectField('代理协议',current.scheme,['http','https','socks5','socks5h']);
 const host=field('代理主机',current.host),port=field('代理端口',current.port,'number');
 port.input.min=1;port.input.max=65535;
 const auth=selectField('代理认证',current.auth_type,[['none','无需认证'],['basic','用户名 / 密码']]);
 const username=field('代理用户名',current.username);
 const password=field('代理密码',current.password,'password','无密码代理可留空；[REDACTED] 表示保留已保存密码，清空可删除密码。');
 password.input.autocomplete='new-password';password.input.setAttribute('aria-label','代理密码');
 const listMode=selectField('名单模式',current.list_mode,[['blacklist','黑名单：名单内直连，其余代理'],['whitelist','白名单：名单内代理，其余直连']]);
 const entries=field('目标名单',current.list_entries.join('\n'),'textarea','每行一个目标：hostname / IP 精确匹配优先，其次 CIDR（只匹配 IP 字面量），最后 *. 通配。例如 api.example.com、192.0.2.0/24、*.example.com（不含主域名）。黑名单为空时全部走代理。');
 entries.input.setAttribute('aria-label','目标名单');
 const ssl=selectField('SSL 验证',current.ssl_verify,[['default','默认验证'],['custom_ca','自定义 CA'],['insecure','关闭验证（临时调试）']]);
 const target=field('代理校验 URL',current.validation_url,'url','连接测试通过代理请求此地址；结果仅供参考，不影响保存。可改为部署环境可达的轻量端点。');
 const warning=el('p',{class:'error',hidden:true},'关闭 SSL 验证存在证书冒用风险，仅用于临时调试。');
 const socks=el('p',{class:'notice',hidden:true},'SOCKS 需要 httpx[socks]（标准安装已包含）；当前 socks5 / socks5h 均由代理解析 DNS。');
 const controls=[mode,scheme,host,port,auth,username,password,listMode,entries,ssl,target];
 const status=el('div',{role:'status','aria-live':'polite',hidden:true});
 const save=el('button',{type:'button',class:'primary'},'保存代理设置');
 const test=el('button',{type:'button'},'测试代理连接');
 const upload=el('input',{type:'file',accept:'.pem,.crt,.cer,application/x-pem-file',hidden:true,'aria-label':'上传 CA 文件'});
 const uploadButton=el('button',{type:'button'},'上传 CA 文件'),remove=el('button',{type:'button',class:'danger'},'移除 CA 文件');
 const caInfo=el('p',{'aria-live':'polite'});
 const ca=el('section',{class:'stack span2',role:'group','aria-label':'自定义 CA 文件'},
  el('h3',{},'自定义 CA 文件'),caInfo,
  el('p',{class:'muted'},'仅支持一个 PEM CA bundle（最大 1 MiB）；多个 CA 请自行合并为 PEM。重新上传将覆盖当前 CA。'),
  el('div',{class:'actions'},uploadButton,remove),upload,
  el('small',{},'切换验证方式会保留文件；移除正在使用的 CA 将恢复默认验证。'));
 let busy=false;
 const sync=()=>{
  const on=enabled.input.checked,manual=mode.input.value==='manual',basic=manual&&auth.input.value==='basic';
  for(const control of controls)control.input.disabled=busy||!on;
  for(const control of [scheme,host,port,auth,username,password,listMode,entries])control.input.disabled=busy||!on||!manual;
  for(const control of [username,password]){control.node.hidden=!basic;control.input.disabled=busy||!on||!basic}
  ca.hidden=ssl.input.value!=='custom_ca';
  upload.disabled=uploadButton.disabled=busy||!on;
  remove.hidden=!current.custom_ca;remove.disabled=busy||!on;
  uploadButton.textContent=current.custom_ca?'重新上传':'上传 CA 文件';
  const metadata=current.custom_ca;
  caInfo.textContent=metadata?(metadata.available?metadata.filename+' · '+date(metadata.uploaded_at)+' 上传成功':metadata.filename+' · 文件不可用，请重新上传'):'尚未上传 CA 文件';
  caInfo.className=metadata?.available?'success':'muted';
  warning.hidden=!on||ssl.input.value!=='insecure';
  socks.hidden=!on||!manual||!scheme.input.value.startsWith('socks');
  enabled.input.disabled=busy;save.disabled=busy;test.disabled=busy||!on;
 };
 const value=()=>({...current,enabled:enabled.input.checked,mode:enabled.input.checked?mode.input.value:'disabled',
  scheme:scheme.input.value,host:host.input.value,port:Number(port.input.value),auth_type:auth.input.value,
  username:username.input.value,password:password.input.value,list_mode:listMode.input.value,
  list_entries:entries.input.value.split(/[\n,]/).map(x=>x.trim()).filter(Boolean),
  ssl_verify:ssl.input.value,validation_url:target.input.value});
 const node=el('section',{class:'card stack outbound-settings'},el('h2',{},'网络与代理'),
  el('p',{class:'notice'},'默认直连并忽略环境变量代理。配置跨 Windows、Linux、macOS 一致生效；保存成功后的下一个出站请求使用新配置，进行中的请求继续完成。'),
  enabled.node,el('div',{class:'form-grid settings-grid'},mode.node,scheme.node,host.node,port.node,auth.node,
   username.node,password.node,listMode.node,entries.node,ssl.node,ca,target.node),
  socks,warning,status,el('div',{class:'actions'},save,test));
 for(const control of [enabled,mode,auth,scheme,ssl])control.input.addEventListener('change',sync);
 node.addEventListener('keydown',event=>{if(event.key==='Enter'&&['INPUT','SELECT'].includes(event.target.tagName)&&event.target.type!=='file'){event.preventDefault();save.click()}});
 const action=async(kind,file)=>{
  if(busy)return;
  busy=true;sync();status.hidden=false;status.className='muted';status.setAttribute('role','status');
  status.textContent=kind==='test'?'测试中…':kind==='upload'?'校验并上传中…':kind==='remove'?'移除中…':'保存中…';
  try{
   if(kind==='test'){
    const result=await api('/settings/outbound-proxy/test',{method:'POST',body:value(),signal});
    status.textContent=result.detail;status.className=result.ok?'success':'notice';
   }else if(kind==='upload'){
    if(file.size>1024*1024)throw new Error('CA 文件不能超过 1 MiB');
    current=await api('/settings/outbound-proxy/ca',{method:'POST',body:{pem:await file.text()},signal});
    status.textContent='CA 文件上传成功';status.className='success';
   }else if(kind==='remove'){
    current=await api('/settings/outbound-proxy/ca',{method:'DELETE',signal});
    ssl.input.value=current.ssl_verify;
    status.textContent='CA 文件已移除，已恢复默认验证';status.className='success';
   }else{
    const draft=value();
    current=await api('/settings/outbound-proxy',{method:'PATCH',body:draft,signal});
    password.input.value=current.password;
    status.textContent=draft.enabled?'代理设置已保存，下一个出站请求生效；可测试连接查看建议':'代理已关闭，出站使用直连';
    status.className='success';
   }
  }catch(error){
   if(error.name!=='AbortError'){status.textContent=error.message;status.className='error';status.setAttribute('role','alert')}
  }finally{busy=false;upload.value='';sync()}
 };
 save.addEventListener('click',()=>action('save'));
 test.addEventListener('click',()=>action('test'));
 uploadButton.addEventListener('click',()=>upload.click());
 upload.addEventListener('change',()=>{const file=upload.files?.[0];if(file)action('upload',file)});
 remove.addEventListener('click',()=>action('remove'));
 sync();return node;
}
