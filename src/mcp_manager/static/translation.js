export const TRANSLATE_CDN='https://cdnjs.webstatic.cn/ajax/libs/translate.js/4.0.0/translate.min.js';

export const DEFAULT_TRANSLATION_CONFIG={
 enabled:false,
 local_language:'chinese_simplified',
 target_language:'english',
 service:'client.edge',
 custom_host:'',
 sse_enabled:false,
 ignore:{class:[],id:[],tag:['code','pre'],text:[]},
 terminology:[],
 url_control:false,
 url_parameter:'language',
 dynamic_content:true,
 whole_page:true,
 translate_local:false,
 queue_enabled:true,
};

export const TRANSLATION_LANGUAGES=[
 ['chinese_simplified','简体中文'],['chinese_traditional','繁體中文'],['english','English'],
 ['japanese','日本語'],['korean','한국어'],['french','Français'],['german','Deutsch'],
 ['spanish','Español'],['portuguese','Português'],['russian','Русский'],['arabic','العربية'],
 ['italian','Italiano'],['dutch','Nederlands'],['thai','ไทย'],['vietnamese','Tiếng Việt'],
 ['indonesian','Bahasa Indonesia'],['turkish','Türkçe'],['polish','Polski'],['ukrainian','Українська'],
];

export function parseList(value){
 const values=Array.isArray(value)?value:String(value??'').split(/[\n,]+/);
 return [...new Set(values.map(item=>String(item).trim()).filter(Boolean))];
}

export function parseTerminology(value){
 if(Array.isArray(value))return value.map(item=>({source:String(item?.source??'').trim(),target:String(item?.target??'').trim()})).filter(item=>item.source&&item.target);
 return String(value??'').split(/\r?\n/).map(line=>{
  const index=line.indexOf('=');return index<1?null:{source:line.slice(0,index).trim(),target:line.slice(index+1).trim()};
 }).filter(item=>item?.source&&item.target);
}

export function normalizeTranslationConfig(value={}){
 const result=structuredClone(DEFAULT_TRANSLATION_CONFIG);
 if(!value||typeof value!=='object'||Array.isArray(value))return result;
 for(const key of Object.keys(result))if(!['ignore','terminology'].includes(key)&&value[key]!==undefined)result[key]=value[key];
 result.enabled=!!result.enabled;
 result.sse_enabled=!!result.sse_enabled;
 result.url_control=!!result.url_control;
 result.dynamic_content=!!result.dynamic_content;
 result.whole_page=!!result.whole_page;
 result.translate_local=!!result.translate_local;
 result.queue_enabled=!!result.queue_enabled;
 result.custom_host=String(result.custom_host??'').trim().replace(/\/+$/,'');
 result.local_language=String(result.local_language||DEFAULT_TRANSLATION_CONFIG.local_language);
 result.target_language=String(result.target_language||DEFAULT_TRANSLATION_CONFIG.target_language);
 result.service=['client.edge','translate.service','giteeAI','custom'].includes(result.service)?result.service:'client.edge';
 result.url_parameter=String(result.url_parameter||'language').trim()||'language';
 const ignore=value.ignore&&typeof value.ignore==='object'?value.ignore:{};
 result.ignore={class:parseList(ignore.class),id:parseList(ignore.id),tag:parseList(ignore.tag),text:parseList(ignore.text)};
 result.terminology=parseTerminology(value.terminology);
 if(result.service!=='custom')result.sse_enabled=false;
 return result;
}

let enginePromise;
const configuredIgnores=new WeakMap();

export function loadTranslationEngine(){
 if(globalThis.translate)return Promise.resolve(globalThis.translate);
 if(enginePromise)return enginePromise;
 enginePromise=new Promise((resolve,reject)=>{
  const existing=document.querySelector('script[data-mmg-translate]');
  const script=existing||document.createElement('script');
  const complete=()=>{
   if(globalThis.translate)resolve(globalThis.translate);
   else{enginePromise=null;reject(new Error('翻译插件加载完成但未提供 translate API'))}
  };
  script.addEventListener('load',complete,{once:true});
  script.addEventListener('error',()=>{enginePromise=null;reject(new Error('翻译插件加载失败，请检查网络或内容安全策略'))},{once:true});
  if(!existing){script.src=TRANSLATE_CDN;script.defer=true;script.dataset.mmgTranslate='true';document.head.append(script)}
 });
 return enginePromise;
}

function configureIgnore(engine,key,values){
 const target=engine.ignore?.[key];
 if(!target||typeof target.push!=='function'||!values.length)return;
 let state=configuredIgnores.get(engine);
 if(!state){state=new Map();configuredIgnores.set(engine,state)}
 const applied=state.get(key)||new Set();
 for(const value of values){
  if(applied.has(value))continue;
  if(Array.isArray(target)&&target.includes(value)){applied.add(value);continue}
  target.push(value);applied.add(value);
 }
 state.set(key,applied);
}

export async function applyTranslation(config,targetLanguage){
 const value=normalizeTranslationConfig(config);
 if(!value.enabled)return{enabled:false,warnings:[]};
 const engine=await loadTranslationEngine(),target=targetLanguage||'english',warnings=[];
 if(engine.selectLanguageTag)engine.selectLanguageTag.show=false;
 engine.language?.setLocal?.(value.local_language);
 engine.service?.use?.(value.service==='custom'?'translate.service':value.service);
 if(value.service==='custom'&&engine.request?.api)value.custom_host&&(engine.request.api.host=value.custom_host+'/');
 for(const key of ['class','id','tag','text'])configureIgnore(engine,key,value.ignore[key]);
 if(engine.nomenclature?.append&&value.terminology.length){
  engine.nomenclature.append(value.local_language,target,value.terminology.map(item=>item.source+'='+item.target).join('\n'));
 }
 if(value.url_control)engine.language?.setUrlParamControl?.(value.url_parameter);
 if(engine.language)engine.language.translateLocal=value.translate_local;
 if(engine.waitingExecute)engine.waitingExecute.use=value.queue_enabled;
 if(value.whole_page)engine.whole?.enableAll?.();
 if(value.dynamic_content)engine.listener?.start?.();
 if(value.sse_enabled){
  if(engine.request?.sse?.start)engine.request.sse.start();
  else warnings.push('当前 translate.js 版本不支持 SSE，已自动使用普通请求。');
 }
 engine.execute?.();
 engine.changeLanguage?.(target);
 return{enabled:true,target,warnings};
}

export async function changeTranslationLanguage(config,target){
 const value=normalizeTranslationConfig(config);
 if(!value.enabled)throw new Error('全局翻译当前已关闭，请由管理员在“翻译设置”中开启。');
 return applyTranslation(value,target);
}

export async function disableTranslation(config){
 const engine=globalThis.translate;
 if(!engine)return false;
 const value=normalizeTranslationConfig(config);
 engine.listener?.stop?.();
 engine.changeLanguage?.(value.local_language);
 return true;
}

export async function clearTranslationCache(){
 let count=0;
 try{
  const keys=[];
  for(let index=0;index<localStorage.length;index++){
   const key=localStorage.key(index);
   if(key&&key.startsWith('hash_'))keys.push(key);
  }
  for(const key of keys){localStorage.removeItem(key);count++}
 }catch{}
 globalThis.translate?.language?.clearCacheLanguage?.();
 return count;
}
