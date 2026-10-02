import{el,button,field,selectField,check,download,toast}from './core.js';
import{
 DEFAULT_TRANSLATION_CONFIG,TRANSLATION_LANGUAGES,normalizeTranslationConfig,
 parseList,parseTerminology,clearTranslationCache
}from './translation.js';

const lines=value=>parseList(value).join('\n');
const terminology=value=>parseTerminology(value).map(item=>item.source+' = '+item.target).join('\n');

export function translationSettings(config){
 let current=normalizeTranslationConfig(config);
 const enabled=check('启用全局网页翻译',current.enabled);
 const local=selectField('网页原始语言',current.local_language,TRANSLATION_LANGUAGES);
 const target=selectField('默认目标语言',current.target_language,TRANSLATION_LANGUAGES);
 const service=selectField('翻译服务通道',current.service,[
  ['client.edge','client.edge（推荐，无需服务端）'],
  ['translate.service','translate.service 公共服务'],
  ['giteeAI','giteeAI 大模型通道'],
  ['custom','自定义私有 translate.service'],
 ]);
 const host=field('私有翻译服务地址',current.custom_host,'url','仅自定义私有服务使用，例如 https://translate.example.com');
 const sse=check('启用 SSE 流式翻译（仅私有服务）',current.sse_enabled);
 const ignoreClass=field('忽略 class',lines(current.ignore.class),'textarea','每行一项，也可以使用英文逗号分隔。');
 const ignoreId=field('忽略 id',lines(current.ignore.id),'textarea');
 const ignoreTag=field('忽略 HTML 标签',lines(current.ignore.tag),'textarea');
 const ignoreText=field('忽略文字',lines(current.ignore.text),'textarea');
 const terms=field('自定义术语',terminology(current.terminology),'textarea','每行 source = target。');
 const urlControl=check('允许 URL 参数控制目标语言',current.url_control);
 const urlParameter=field('URL 语言参数名',current.url_parameter,'text','默认 language，例如 ?language=english');
 const dynamic=check('监控动态渲染内容',current.dynamic_content);
 const whole=check('启用整页整体翻译',current.whole_page);
 const translateLocal=check('允许翻译本地语种文本',current.translate_local);
 const queue=check('启用翻译任务排队',current.queue_enabled);
 const customBox=el('div',{class:'stack translation-custom'},host.node,sse.node,
  el('p',{class:'muted'},'SSE 仅适用于兼容版本的私有 translate.service；当前插件不支持时会自动回退普通请求。'));
 const updateVisibility=()=>{customBox.hidden=service.input.value!=='custom';if(customBox.hidden)sse.input.checked=false};
 service.input.addEventListener('change',updateVisibility);updateVisibility();

 const value=()=>normalizeTranslationConfig({
  enabled:enabled.input.checked,
  local_language:local.input.value,
  target_language:target.input.value,
  service:service.input.value,
  custom_host:host.input.value,
  sse_enabled:sse.input.checked,
  ignore:{class:parseList(ignoreClass.input.value),id:parseList(ignoreId.input.value),tag:parseList(ignoreTag.input.value),text:parseList(ignoreText.input.value)},
  terminology:parseTerminology(terms.input.value),
  url_control:urlControl.input.checked,
  url_parameter:urlParameter.input.value,
  dynamic_content:dynamic.input.checked,
  whole_page:whole.input.checked,
  translate_local:translateLocal.input.checked,
  queue_enabled:queue.input.checked,
 });

 const setValue=input=>{
  current=normalizeTranslationConfig(input);
  enabled.input.checked=current.enabled;local.input.value=current.local_language;target.input.value=current.target_language;
  service.input.value=current.service;host.input.value=current.custom_host;sse.input.checked=current.sse_enabled;
  ignoreClass.input.value=lines(current.ignore.class);ignoreId.input.value=lines(current.ignore.id);
  ignoreTag.input.value=lines(current.ignore.tag);ignoreText.input.value=lines(current.ignore.text);
  terms.input.value=terminology(current.terminology);urlControl.input.checked=current.url_control;
  urlParameter.input.value=current.url_parameter;dynamic.input.checked=current.dynamic_content;
  whole.input.checked=current.whole_page;translateLocal.input.checked=current.translate_local;
  queue.input.checked=current.queue_enabled;updateVisibility();
 };

 const upload=el('input',{type:'file',accept:'application/json,.json',hidden:true,'aria-label':'导入翻译配置文件'});
 upload.addEventListener('change',async()=>{
  const file=upload.files?.[0];if(!file)return;
  try{setValue(JSON.parse(await file.text()));toast('翻译配置已载入，请保存后生效','success')}
  catch(error){toast('无法导入翻译配置：'+error.message,'fail')}
  finally{upload.value=''}
 });
 const actions=el('div',{class:'actions'},
  button('导出翻译配置',()=>download('mcp-manager-translation.json',value())),
  button('导入翻译配置',()=>upload.click()),
  button('清除翻译缓存',async()=>{await clearTranslationCache();toast('翻译缓存已清除','success')}),
  button('重置翻译设置',()=>{setValue(DEFAULT_TRANSLATION_CONFIG);toast('已恢复默认值，请保存后生效','info')},'danger'),
  upload
 );
 const node=el('section',{class:'card stack translation-settings'},
  el('div',{},el('h2',{},'翻译设置'),el('p',{class:'notice'},'页面翻译由 translate.js 提供。启用后，页面文字会按所选通道发送给对应翻译服务。')),
  enabled.node,
  el('div',{class:'form-grid settings-grid'},local.node,target.node,service.node),
  customBox,
  el('section',{class:'stack'},el('h3',{},'忽略规则'),el('div',{class:'form-grid settings-grid'},ignoreClass.node,ignoreId.node,ignoreTag.node,ignoreText.node)),
  el('section',{class:'stack'},el('h3',{},'自定义术语'),terms.node),
  el('section',{class:'stack'},el('h3',{},'高级与性能'),el('div',{class:'form-grid settings-grid'},urlParameter.node),urlControl.node,dynamic.node,whole.node,translateLocal.node,queue.node),
  actions
 );
 return{node,value,setValue};
}
