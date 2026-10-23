// No browser packages required. Exercise the shipped app's startup against
// parser/SDK timing and connection failure, independent of backend unit tests.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {setTimeout: delay} = require('node:timers/promises');

class Element {
  constructor(tag='div') { this.tagName=tag.toUpperCase(); this.children=[]; this.dataset={}; this.classList={toggle(){}}; this.textContent=''; }
  append(...items) { this.children.push(...items); }
  prepend(...items) { this.children.unshift(...items); }
  replaceChildren(...items) { this.children=[...items]; if(this.tagName==='SELECT')this._value=undefined; }
  removeAttribute(name) { delete this[name]; }
  get childNodes() { return this.children; }
  get firstChild() { return this.children[0]; }
  get options() { return this.children; }
  get value() { return this._value??(this.tagName==='SELECT'?this.firstChild?.value:'')??''; }
  set value(value) { this._value=value; }
}
const page=path.join(__dirname,'../pages/控制台');
const app=fs.readFileSync(path.join(page,'app.js'),'utf8');
const html=fs.readFileSync(path.join(page,'index.html'),'utf8');
function setup(readyState='loading') {
  const elements=new Map([...html.matchAll(/<([\w-]+)[^>]*\bid="([^"]+)"/g)].map(m=>[m[2],new Element(m[1])]));
  const listeners=new Map();
  const document={readyState,getElementById:id=>elements.get(id),createElement:tag=>new Element(tag),createTextNode:text=>text,
    addEventListener:(type,fn)=>listeners.set(type,fn),querySelectorAll:selector=>selector.startsWith('#test')?
      [...elements].filter(([id,e])=>!['refresh'].includes(id)&&['BUTTON','INPUT','SELECT','TEXTAREA'].includes(e.tagName)).map(([,e])=>e):[]};
  elements.get('kind').value='share'; elements.get('source').value='random';
  const window={}; const errors=[];
  vm.runInNewContext(app,{document,window,Date,Map,Promise,setTimeout,clearTimeout,crypto:require('node:crypto').webcrypto,console:{error:e=>errors.push(e)}});
  return {elements,window,errors,async parsed(){document.readyState='complete';await listeners.get('DOMContentLoaded')?.();await delay(0);}};
}
const UMO='test:GroupMessage:123456789';
function sdk({fail=false,envelope=false,share=false,imageError=false,twoGroups=false,secondImageError=false,sendError=false,captionError=false,settings=false}={}) {
  const calls=[], bodies=[];
  let imageWording='画了张街角';
  const groups=[{umo:UMO,name:'测试群',count:0,cap:3,manual_count:3,manual_cap:20,calls:0,call_cap:8,paused:false,blockers:[],plan:[],pending:[],attempts:[]}];
  if(twoGroups)groups.push({...groups[0],umo:'arona:GroupMessage:999999',name:'第二个测试群'});
  const imageBudget={used:2,limit:20,remaining:18,interval_seconds:60};
  const status={status:'ok',data:{version:'test',groups,total_calls:0,total_call_cap:16,config:{},schema:{},secrets:{},revision:'r',image_test_budget:imageBudget}};
  if(settings){status.data.schema=JSON.parse(fs.readFileSync(path.join(__dirname,'../_conf_schema.json'),'utf8'));
    status.data.config=Object.fromEntries(Object.entries(status.data.schema).filter(([key])=>!key.endsWith('_api_key')).map(([key,spec])=>[key,spec.default]));
    Object.assign(status.data.config,{provider_id:'test-provider',daily_limit:'1-3',quiet_hours:'23:00-08:00',image_test_daily_limit:20});}
  // Match the installed Views host: prefix the plugin, reject HTTP error
  // envelopes and deliver response.data.data ?? response.data to the SDK.
  function route(endpoint) {
    const url='/api/v1/plugins/extensions/astrbot_plugin_acg_companion/'+endpoint.replace(/^\/+|\/+$/g,'');
    calls.push(url);
    if(!['status','preview','send','pause','config','image-start','image-status','image-caption'].includes(endpoint))throw new Error('未找到该路由');
  }
  const unwrap=result=>envelope?result:(result.data??result);
  return {calls,bodies,ready:()=>Promise.resolve(),apiGet:async endpoint=>{route(endpoint);if(fail)throw new Error('后台暂时不可用');return unwrap(status);},
    apiPost:async(endpoint,body)=>{route(endpoint);bodies.push({endpoint,body});let result;
      if(endpoint==='send'){if(sendError)throw new Error('本群今日手动试发已用 20/20 次；可在插件设置提高上限');result={status:'ok',data:{result:'sent',umo:UMO},message:'已发送到选定群'};}
      else if(endpoint==='image-start')result={status:'ok',data:{token:'image-job',status:'running'}};
      else if(endpoint==='image-caption'){if(captionError)throw new Error('今日文本模型额度已用完');imageWording='今天吃了碗面，终于暖和过来了';result={status:'ok',data:{text:imageWording}};}
      else if(endpoint==='image-status')result={status:'ok',data:imageError?{status:'error',message:'密钥无效或已过期'}:secondImageError&&bodies.filter(x=>x.endpoint==='image-start').length>1?{status:'error',message:'达到插件设置的每日上限',budget:imageBudget}:{status:'ready',budget:imageBudget,preview:{token:'image-draft',text:imageWording,title:'涂鸦',umo:UMO,image:'data:image/png;base64,offline',expires:Date.now()/1000+600,sendable:true,reused:false}}};
      else if(endpoint==='config')result={...status,message:'已保存并立即生效'};
      else if(endpoint==='pause')result={status:'ok',data:null,message:body.paused?'已暂停':'已恢复'};
      else result={status:'ok',data:share?{decision:'send',text:'预览文本',token:'test-token',sendable:true,expires:Date.now()/1000+600,umo:body.umo,
        card:{title:'新番 OP',author:'测试歌手',source:'QQ 音乐',comment:'预览文本',url:'https://y.qq.com/n/ryqq/songDetail/test123',cover:'https://y.gtimg.cn/test.jpg'}}:{decision:'skip',sendable:false,reason:'语境不适合',simulation:false,umo:body.umo}};
      return unwrap(result);
    }};
}
test('SDK appended after app.js connects after parsing and can preview',async()=>{
  const ui=setup();
  assert.equal(ui.elements.get('generate').disabled,true);
  const bridge=sdk(); ui.window.AstrBotPluginView=bridge;
  await ui.parsed();
  assert.equal(ui.elements.get('connection').textContent,'已连接 AstrBot');
  assert.equal(ui.elements.get('generate').disabled,false);
  await ui.elements.get('generate').onclick();
  assert.equal(bridge.calls.filter(x=>x.endsWith('/preview')).length,1);
  assert.equal(ui.elements.get('preview-text').textContent,'这次它决定先不说。');
  assert.deepEqual(ui.errors,[]);
});
test('missing SDK keeps mutations disabled with a recoverable error',async()=>{
  const ui=setup('complete');
  // Even calling the handler directly cannot reach apiPost before connection.
  await ui.elements.get('generate').onclick();
  await delay(5200);
  assert.equal(ui.elements.get('generate').disabled,true);
  assert.equal(ui.elements.get('save').disabled,true);
  assert.equal(ui.elements.get('send').disabled,true);
  assert.equal(ui.elements.get('refresh').textContent,'重新连接');
  assert.match(ui.elements.get('notice').textContent,/未加载到 AstrBot 页面接口/);
  ui.window.AstrBotPluginPage=sdk();
  await ui.elements.get('refresh').onclick();
  assert.equal(ui.elements.get('connection').textContent,'已连接 AstrBot');
  assert.equal(ui.elements.get('generate').disabled,false);
});
test('backend unavailable does not unlock actions; reconnect recovers',async()=>{
  const ui=setup(); ui.window.AstrBotPluginView=sdk({fail:true}); await ui.parsed();
  assert.match(ui.elements.get('notice').textContent,/后台暂时不可用/);
  assert.equal(ui.elements.get('generate').disabled,true);
  assert.equal(ui.elements.get('save').disabled,true);
  ui.window.AstrBotPluginView=sdk(); await ui.elements.get('refresh').onclick();
  assert.equal(ui.elements.get('connection').textContent,'已连接 AstrBot');
  assert.equal(ui.elements.get('generate').disabled,false);
});
test('SDK loaded late after parsing is awaited',async()=>{
  const ui=setup('complete');
  assert.equal(ui.elements.get('generate').disabled,true);
  await delay(150); ui.window.AstrBotPluginView=sdk(); await delay(200);
  assert.equal(ui.elements.get('connection').textContent,'已连接 AstrBot');
  assert.equal(ui.elements.get('generate').disabled,false);
});
test('unwrapped preview and send payloads work through plugin-prefixed routes',async()=>{
  const ui=setup(); const bridge=sdk({share:true});ui.window.AstrBotPluginView=bridge;await ui.parsed();
  await ui.elements.get('generate').onclick();
  assert.equal(ui.elements.get('preview-text').textContent,'预览文本');
  assert.equal(ui.elements.get('preview-text').hidden,false);
  assert.equal(ui.elements.get('share-card').hidden,false);
  assert.equal(ui.elements.get('share-title').textContent,'新番 OP');
  assert.equal(ui.elements.get('share-cover').src,'https://y.gtimg.cn/test.jpg');
  assert.equal(ui.elements.get('send').disabled,false);
  await ui.elements.get('send').onclick();
  assert.equal(ui.elements.get('notice').textContent,'已发送到选定群');
  assert.equal(ui.elements.get('send').disabled,true);
  await ui.elements.get('send').onclick();
  assert.equal(bridge.calls.filter(x=>x.endsWith('/send')).length,1);
  assert.equal(bridge.calls.filter(x=>x.endsWith('/preview')).length,1);
  ui.elements.get('source').onchange();
  assert.equal(ui.elements.get('share-card').hidden,true);
  assert.equal(ui.elements.get('share-cover').src,undefined);
});

test('broken cover keeps title and comment readable',async()=>{
  const ui=setup();ui.window.AstrBotPluginView=sdk({share:true});await ui.parsed();
  await ui.elements.get('generate').onclick();
  ui.elements.get('share-cover').onerror();
  assert.equal(ui.elements.get('share-cover').hidden,true);
  assert.equal(ui.elements.get('share-placeholder').hidden,false);
  assert.equal(ui.elements.get('share-title').textContent,'新番 OP');
  assert.equal(ui.elements.get('send').disabled,false);
});
test('unwrapped settings response still shows a save confirmation',async()=>{
  const ui=setup();const bridge=sdk();ui.window.AstrBotPluginView=bridge;await ui.parsed();
  ui.elements.get('settings-form').onsubmit({preventDefault(){}});await delay(0);
  assert.equal(bridge.calls.filter(x=>x.endsWith('/config')).length,1);
  assert.equal(ui.elements.get('notice').textContent,'已保存并立即生效');
  await ui.elements.get('pause').onclick();
  assert.equal(ui.elements.get('notice').textContent,'已暂停');
});

test('collapsed settings preserve every field and submit merged frequency values',async()=>{
  const ui=setup();const bridge=sdk({settings:true});ui.window.AstrBotPluginView=bridge;await ui.parsed();
  const sections=ui.elements.get('config-fields').children;
  assert.deepEqual(sections.map(x=>x.tagName),['DIV','DIV','DETAILS','DETAILS']);
  assert.equal(sections[2].open,undefined);
  ui.elements.get('settings-form').onsubmit({preventDefault(){}});await delay(0);
  const body=bridge.bodies.find(x=>x.endpoint==='config').body;
  assert.equal(Object.keys(body.values).length,28);
  assert.equal(body.values.daily_limit,'1-3');assert.equal(body.values.quiet_hours,'23:00-08:00');
  assert.equal(body.values.image_test_daily_limit,20);
  assert.equal(body.values.image_api_key,'');
  assert.equal(body.values.daily_min,undefined);assert.equal(body.values.share_native_card,undefined);
});
test('envelope responses from older hosts remain supported',async()=>{
  const ui=setup();ui.window.AstrBotPluginView=sdk({envelope:true});await ui.parsed();
  assert.equal(ui.elements.get('connection').textContent,'已连接 AstrBot');
  await ui.elements.get('generate').onclick();
  assert.equal(ui.elements.get('preview-text').textContent,'这次它决定先不说。');
});

test('image preview uses independent routes and send reuses the draft',async()=>{
  const ui=setup();const bridge=sdk();ui.window.AstrBotPluginView=bridge;await ui.parsed();
  assert.equal(ui.elements.get('image-generate').disabled,false);
  await ui.elements.get('image-generate').onclick();
  assert.equal(ui.elements.get('image-preview').hidden,false);
  assert.equal(ui.elements.get('image-text').textContent,'画了张街角');
  assert.match(ui.elements.get('image-meta').textContent,/123456789/);
  assert.equal(ui.elements.get('image-send').disabled,false);
  assert.equal(bridge.calls.filter(x=>x.endsWith('/preview')).length,0);
  await ui.elements.get('image-send').onclick();
  await ui.elements.get('image-send').onclick();
  assert.equal(bridge.calls.filter(x=>x.endsWith('/send')).length,1);
  assert.equal(bridge.calls.filter(x=>x.endsWith('/image-start')).length,1);
});
test('image generation error is visible and never unlocks send or resubmits',async()=>{
  const ui=setup();const bridge=sdk({imageError:true});ui.window.AstrBotPluginView=bridge;await ui.parsed();
  await ui.elements.get('image-generate').onclick();
  assert.match(ui.elements.get('image-note').textContent,/密钥无效/);
  assert.equal(ui.elements.get('image-send').disabled,true);
  await ui.elements.get('image-check').onclick();
  assert.equal(bridge.calls.filter(x=>x.endsWith('/image-start')).length,1);
});

test('image test selects its own group and send remains pinned to the preview',async()=>{
  const ui=setup();const bridge=sdk({twoGroups:true});ui.window.AstrBotPluginView=bridge;await ui.parsed();
  ui.elements.get('image-group').value='arona:GroupMessage:999999';
  await ui.elements.get('image-generate').onclick();
  assert.equal(bridge.bodies.find(x=>x.endpoint==='image-start').body.umo,'arona:GroupMessage:999999');
  ui.elements.get('image-group').value=UMO;
  await ui.elements.get('image-send').onclick();
  const sent=bridge.bodies.find(x=>x.endpoint==='send').body;
  assert.equal(sent.token,'image-draft');
  assert.equal(sent.umo,undefined);
  assert.equal(ui.elements.get('image-group').value,UMO);
});

test('image budget reflects configured limit and failed new test preserves the good preview',async()=>{
  const ui=setup();const bridge=sdk({secondImageError:true});ui.window.AstrBotPluginView=bridge;await ui.parsed();
  assert.match(ui.elements.get('image-budget').textContent,/2\/20/);
  await ui.elements.get('image-generate').onclick();
  const firstImage=ui.elements.get('image-preview').src;
  ui.elements.get('image-fresh').checked=true;
  await ui.elements.get('image-generate').onclick();
  assert.equal(ui.elements.get('image-preview').hidden,false);
  assert.equal(ui.elements.get('image-preview').src,firstImage);
  assert.match(ui.elements.get('image-note').textContent,/上一张预览已保留/);
  assert.equal(ui.elements.get('image-send').disabled,false);
  await ui.elements.get('image-send').onclick();
  assert.equal(bridge.bodies.find(x=>x.endpoint==='send').body.token,'image-draft');
  assert.equal(bridge.calls.filter(x=>x.endsWith('/image-start')).length,2);
});

test('image send rejection appears beside the image and progress can recover the unconsumed preview',async()=>{
  const ui=setup();const bridge=sdk({sendError:true});ui.window.AstrBotPluginView=bridge;await ui.parsed();
  await ui.elements.get('image-generate').onclick();
  assert.match(ui.elements.get('image-send-budget').textContent,/3\/20/);
  await ui.elements.get('image-send').onclick();
  assert.match(ui.elements.get('image-note').textContent,/20\/20/);
  assert.match(ui.elements.get('image-note').textContent,/检查生图进度/);
  assert.equal(ui.elements.get('image-preview').hidden,false);
  assert.equal(ui.elements.get('image-send').disabled,true);
  await ui.elements.get('image-send').onclick();
  assert.equal(bridge.calls.filter(x=>x.endsWith('/send')).length,1);
  await ui.elements.get('image-check').onclick();
  assert.equal(ui.elements.get('image-send').disabled,false);
  assert.equal(bridge.calls.filter(x=>x.endsWith('/image-start')).length,1);
});

test('pending image send is visible next to the button and cannot submit twice',async()=>{
  const ui=setup();const bridge=sdk();const original=bridge.apiPost;let release;
  const gate=new Promise(resolve=>{release=resolve;});
  bridge.apiPost=async(endpoint,body)=>{if(endpoint==='send')await gate;return original(endpoint,body);};
  ui.window.AstrBotPluginView=bridge;await ui.parsed();
  await ui.elements.get('image-generate').onclick();
  const sending=ui.elements.get('image-send').onclick();
  await delay(0);
  assert.match(ui.elements.get('image-note').textContent,/正在发送/);
  assert.equal(ui.elements.get('image-send').disabled,true);
  await ui.elements.get('image-send').onclick();
  release();await sending;
  assert.match(ui.elements.get('image-note').textContent,/已发送/);
  assert.equal(bridge.calls.filter(x=>x.endsWith('/send')).length,1);
});

test('caption rewrite changes only wording and keeps the same image and send token',async()=>{
  const ui=setup();const bridge=sdk();ui.window.AstrBotPluginView=bridge;await ui.parsed();
  assert.equal(ui.elements.get('image-rewrite').disabled,true);
  await ui.elements.get('image-generate').onclick();
  const image=ui.elements.get('image-preview').src;
  ui.elements.get('image-prompt').value='热腾腾的一碗面';
  await ui.elements.get('image-rewrite').onclick();
  assert.equal(ui.elements.get('image-text').textContent,'今天吃了碗面，终于暖和过来了');
  assert.equal(ui.elements.get('image-preview').src,image);
  assert.match(ui.elements.get('image-note').textContent,/图片未重新生成/);
  const rewrite=bridge.bodies.find(x=>x.endpoint==='image-caption').body;
  assert.equal(rewrite.token,'image-draft');assert.equal(rewrite.theme,'热腾腾的一碗面');
  assert.equal(bridge.calls.filter(x=>x.endsWith('/image-start')).length,1);
  assert.equal(bridge.calls.filter(x=>x.endsWith('/send')).length,0);
  await ui.elements.get('image-send').onclick();
  assert.equal(bridge.bodies.find(x=>x.endpoint==='send').body.token,'image-draft');
  assert.equal(ui.elements.get('image-rewrite').disabled,true);
});

test('caption rewrite failure preserves the previous wording and ready image',async()=>{
  const ui=setup();const bridge=sdk({captionError:true});ui.window.AstrBotPluginView=bridge;await ui.parsed();
  await ui.elements.get('image-generate').onclick();
  const image=ui.elements.get('image-preview').src,text=ui.elements.get('image-text').textContent;
  await ui.elements.get('image-rewrite').onclick();
  assert.equal(ui.elements.get('image-preview').src,image);
  assert.equal(ui.elements.get('image-text').textContent,text);
  assert.match(ui.elements.get('image-note').textContent,/额度已用完/);
  assert.equal(ui.elements.get('image-send').disabled,false);
  assert.equal(bridge.calls.filter(x=>x.endsWith('/image-start')).length,1);
});
