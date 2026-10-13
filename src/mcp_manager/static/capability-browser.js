import{api,badge,button,dialog,el,empty,loading,pretty}from './core.js';

export const CAPABILITY_PAGE_SIZE=50;
export const capabilityKinds=[
 ['tools','工具'],['resources','资源'],['prompts','提示词'],['templates','资源模板']
];

export function capabilityListUrl(resource,{kind='tools',q='',cursor=null,limit=CAPABILITY_PAGE_SIZE}={}){
 const params=new URLSearchParams({kind,q,limit:String(Math.min(CAPABILITY_PAGE_SIZE,Math.max(1,limit)))});
 if(cursor)params.set('cursor',cursor);
 return resource+'?'+params;
}

export function capabilityDetailUrl(resource,kind,name){
 return resource+'/'+encodeURIComponent(kind)+'/'+encodeURIComponent(name);
}

export function capabilityBrowser({resource,signal,title='能力目录',initialKind='tools',onTool,actions=[]}){
 const owned=new AbortController();
 if(signal){
  if(signal.aborted)owned.abort();
  else signal.addEventListener('abort',()=>owned.abort(),{once:true});
 }
 let kind=initialKind,q='',epoch=0,current=null,cursors=[null],page=0,data={items:[],total:0,next_cursor:null};
 const tabs=el('div',{class:'tabs capability-tabs',role:'tablist','aria-label':'能力类型'});
 const search=el('input',{type:'search',placeholder:'搜索当前能力…','aria-label':'搜索当前能力'});
 const form=el('form',{class:'toolbar capability-toolbar'},search,el('button',{type:'submit'},'搜索'));
 const body=el('div',{class:'capability-list','aria-live':'polite'});
 const foot=el('div',{class:'pagination capability-pagination'});
 const shell=el('div',{class:'stack capability-browser',role:'region'},actions.length?el('div',{class:'actions capability-actions'},...actions):null,tabs,form,body,foot);
 const popup=dialog(title,shell);
 popup.addEventListener('close',()=>owned.abort(),{once:true});

 function nameOf(item){return item.name||item.uri||item.uriTemplate||'未命名能力'}
 function tabState(){
  const label=capabilityKinds.find(([value])=>value===kind)?.[1]||'能力';
  shell.setAttribute('aria-label',label+'列表');
  search.setAttribute('aria-label','搜索'+label);
  form.setAttribute('aria-label',label+'筛选');
  [...tabs.children].forEach(node=>{
   const selected=node.dataset.kind===kind;
   node.classList.toggle('active',selected);
   node.setAttribute('aria-selected',String(selected));
   node.tabIndex=selected?0:-1;
  });
 }
 async function showDefinition(item,card,control){
  const shown=card.querySelector('.capability-definition[data-loaded="true"]');
  if(shown){shown.remove();control.querySelector('.button-label').textContent='查看完整定义';control.setAttribute('aria-expanded','false');return}
  const request=++epoch;
  current?.abort();current=new AbortController();
  const relay=()=>current.abort();
  owned.signal.addEventListener('abort',relay,{once:true});
  control.disabled=true;control.setAttribute('aria-busy','true');
  let area=card.querySelector('.capability-definition');
  if(!area){area=el('div',{class:'capability-definition'});card.append(area)}
  area.replaceChildren(loading('正在获取完整定义…'));
  try{
   const value=await api(capabilityDetailUrl(resource,kind,nameOf(item)),{signal:current.signal});
   if(request!==epoch||owned.signal.aborted||!card.isConnected)return;
   area.dataset.loaded='true';area.replaceChildren(el('pre',{},pretty(value)));
   control.querySelector('.button-label').textContent='收起定义';control.setAttribute('aria-expanded','true');
   if(kind==='tools'&&onTool)card.querySelector('[data-test-tool]')?.removeAttribute('disabled');
  }catch(error){
   if(error.name!=='AbortError'&&request===epoch)area.replaceChildren(el('div',{class:'error',role:'alert'},error.message));
  }finally{
   owned.signal.removeEventListener('abort',relay);
   if(request===epoch){control.disabled=false;control.removeAttribute('aria-busy')}
  }
 }
 function render(){
  body.replaceChildren();
  for(const item of data.items.slice(0,CAPABILITY_PAGE_SIZE)){
   const card=el('article',{class:'card capability-card'});
   const detailButton=button('查看完整定义',()=>showDefinition(item,card,detailButton));
   detailButton.setAttribute('aria-expanded','false');
   card.append(
    el('div',{class:'capability-heading'},el('strong',{class:'capability-name'},nameOf(item)),
     item.annotations?.readOnlyHint?badge('只读'):null),
    el('p',{class:'muted'},item.description||item.mimeType||'暂无描述'),
    el('div',{class:'actions'},detailButton,
     kind==='tools'&&onTool?button('调用测试',async()=>{
      const definition=await api(capabilityDetailUrl(resource,kind,nameOf(item)),{signal:owned.signal});
      onTool(definition);
     }):null)
   );
   body.append(card);
  }
  if(!data.items.length)body.append(empty(q?'没有匹配能力。':'当前没有可展示的能力。'));
  foot.replaceChildren();
  const previous=button('上一页',()=>{if(page){page--;load()}});
  const next=button('下一页',()=>{if(data.next_cursor){page++;cursors[page]=data.next_cursor;load()}});
  previous.disabled=page===0;next.disabled=!data.next_cursor;
  foot.append(el('span',{class:'total'},'共 '+data.total+' 项 · 第 '+(page+1)+' 页'),previous,next);
 }
 async function load(){
  const request=++epoch;
  current?.abort();current=new AbortController();
  const relay=()=>current.abort();
  owned.signal.addEventListener('abort',relay,{once:true});
  body.setAttribute('aria-busy','true');
  body.replaceChildren(loading('正在加载能力摘要…'));foot.replaceChildren();
  try{
   const result=await api(capabilityListUrl(resource,{kind,q,cursor:cursors[page]}),{signal:current.signal});
   if(request!==epoch||owned.signal.aborted||!popup.isConnected)return;
   data=result;render();
  }catch(error){
   if(error.name!=='AbortError'&&request===epoch)body.replaceChildren(el('div',{class:'empty'},el('p',{class:'error'},error.message),button('重试',load)));
  }finally{
   owned.signal.removeEventListener('abort',relay);
   if(request===epoch)body.removeAttribute('aria-busy');
  }
 }
 for(const [value,label]of capabilityKinds){
  const tab=button(label,()=>{if(kind===value)return;kind=value;page=0;cursors=[null];tabState();load()});
  tab.dataset.kind=value;tab.setAttribute('role','tab');tabs.append(tab);
 }
 tabState();
 form.addEventListener('submit',event=>{event.preventDefault();q=search.value.trim();page=0;cursors=[null];load()});
 load();
 return popup;
}
