(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const labels = {share:'主动分享',purchase:'购物玩笑',social:'联机 / 生活提问',problem:'问题跟进',thursday:'星期四文案'};
  const states = {sent:'已发送',uncertain:'结果不确定，未重发',reserved:'发送处理中',manual_test:'已手动处理'};
  const samples = {purchase:'我刚买的游戏手柄到货了，手感还不错',social:'今晚有没有人一起联机打游戏？我晚饭后有空',problem:'游戏更新之后一直连不上服务器，试了重启也不行，大家有办法吗？',thursday:''};
  let bridge, state, draft, busy = false, connected = false, connecting = false, revision, inputs = new Map(), keyClears = new Map();
  let imageDraft, imageJob, imageRunning=false, imageTimer;
  function node(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
  function notice(text, error=false) { $('notice').textContent=text; $('notice').className=error?'error':''; }
  function invalidate() { draft=null; $('send').disabled=true; $('preview-meta').textContent='尚未生成'; $('preview-text').hidden=false; $('preview-text').textContent='先生成一条预览，确认效果后再试发。'; $('preview-note').textContent=''; renderCard(null); }
  function invalidateImage() { imageDraft=null;imageJob=null;imageRunning=false;clearTimeout(imageTimer);$('image-preview').hidden=true;$('image-preview').removeAttribute('src');$('image-text').hidden=true;$('image-meta').textContent='尚未生成';$('image-note').textContent='仅点击生成不会向群里发消息。'; }
  function renderCard(card) {
    const box=$('share-card'),image=$('share-cover');box.hidden=!card;box.removeAttribute('href');image.hidden=true;image.removeAttribute('src');
    $('share-placeholder').hidden=false;
    if(!card)return;
    box.href=card.url;
    $('share-title').textContent=card.title;$('share-author').textContent=card.author||'';$('share-source').textContent=card.source;
    if(card.cover){image.onload=()=>{$('share-placeholder').hidden=true;};image.onerror=()=>{image.hidden=true;$('share-placeholder').hidden=false;};image.src=card.cover;image.hidden=false;}
  }
  function group() { return state?.groups.find(g=>g.umo===$('group').value); }
  function fmt(ts) { return new Date(ts*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit'}); }
  function renderImageBudget(budget) {
    if(!budget)return;
    $('image-budget').textContent=`今日手动生图已提交 ${budget.used}/${budget.limit} 次，还可提交 ${budget.remaining} 次；两次至少间隔 ${budget.interval_seconds} 秒。失败也计入。次数和间隔可在“插件设置”调整，服务端额度另计。`;
  }
  function renderManualBudget() {
    const g=state?.groups.find(g=>g.umo===(imageDraft?.umo||$('image-group').value));
    $('image-send-budget').textContent=g?`预览目标群今日手动试发 ${g.manual_count??0}/${g.manual_cap??10} 次；与自动发言、生图请求次数分开。`:'';
  }
  function withTimeout(promise, ms, message) {
    let timer;
    return Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error(message)),ms);})]).finally(()=>clearTimeout(timer));
  }
  async function api(path, body) {
    if(typeof bridge?.apiGet!=='function'||typeof bridge?.apiPost!=='function')throw new Error('尚未连接 AstrBot，请点击右上角“重新连接”。');
    // AstrBot's host adds the plugin prefix and unwraps the HTTP envelope.
    // SDK endpoints are relative, e.g. "status", never /api/plug/<plugin>/status.
    const response=body===undefined?bridge.apiGet(path):bridge.apiPost(path,body);
    const raw=await withTimeout(response,path==='preview'?110000:path==='send'?55000:path==='image-caption'?35000:20000,path==='send'?'发送等待超时，请到群里确认；本页不会自动重发。':'请求等待超时，请确认 AstrBot 正在运行；本页不会自动重试。');
    let result=raw;
    // Older/test hosts may retain the envelope; current Views return data only.
    if (raw?.data?.status) result=raw.data;
    if(result?.status==='error'||result?.status==='warning')throw new Error(result.message||'请求失败，请刷新页面后再试');
    if(result?.status==='ok')return result;
    if(result===null||result===undefined)throw new Error('AstrBot 返回了空结果，请刷新状态后再试。');
    return {status:'ok',data:result,message:''};
  }
  function syncControls() {
    document.querySelectorAll('#test button,#test input,#test select,#test textarea,#image-test button,#image-test input,#image-test select,#image-test textarea,#settings button,#settings input,#settings select,#settings textarea').forEach(el=>el.disabled=!connected||busy);
    $('send').disabled=!connected||busy||!draft?.sendable;
    $('generate').disabled=!connected||busy||!group();
    $('image-generate').disabled=!connected||busy||imageRunning||!state?.groups.some(g=>g.umo===$('image-group').value);
    $('image-check').disabled=!connected||busy||!imageJob;
    $('image-send').disabled=!connected||busy||imageRunning||!imageDraft?.sendable;
    $('image-rewrite').disabled=!connected||busy||imageRunning||!imageDraft?.sendable;
    $('pause').disabled=!connected||busy||!group();
    $('refresh').disabled=busy||connecting;
    $('refresh').textContent=connecting?'正在连接…':connected?'刷新状态':'重新连接';
    $('connection').textContent=connecting?'连接中':connected?'已连接 AstrBot':'未连接';
    $('connection').dataset.state=connected?'ready':connecting?'pending':'error';
    renderManualBudget();
  }
  function setBusy(value) { busy=value; syncControls(); }
  async function action(fn) {
    if(busy||connecting)return;
    if(!connected){notice('尚未连接 AstrBot，请点击右上角“重新连接”。',true);return;}
    setBusy(true);try{await fn();}catch(e){notice(e.message||'操作失败，请刷新状态后再试。',true);}finally{setBusy(false);}
  }
  async function connect() {
    if(connecting||busy)return;
    connecting=true;connected=false;invalidate();syncControls();notice('正在连接 AstrBot 管理后台…');
    try {
      // AstrBot normally appends its SDK after app.js. Start after HTML parsing,
      // and also tolerate a late SDK load instead of leaving live buttons behind.
      bridge=await new Promise((resolve,reject)=>{
        const deadline=Date.now()+5000;
        function check(){const sdk=window.AstrBotPluginView||window.AstrBotPluginPage;
          if(typeof sdk?.ready==='function'&&typeof sdk?.apiGet==='function'&&typeof sdk?.apiPost==='function')return resolve(sdk);
          if(Date.now()>=deadline)return reject(new Error('未加载到 AstrBot 页面接口。请从“插件管理 → ACG 群友 → Views → 控制台”重新打开，勿直接打开 HTML 文件。'));
          setTimeout(check,100);
        }check();
      });
      await withTimeout(bridge.ready(),8000,'AstrBot 页面连接超时，请关闭此视图后，从插件详情重新打开。');
      await load(true);connected=true;notice('已连接。先生成预览，确认后再发送；生成预览会计入模型额度。');
    }catch(e){notice(e.message||'连接失败，请从 AstrBot 插件详情重新打开控制台。',true);}
    finally{connecting=false;syncControls();}
  }
  async function load(config=false) {
    const result=await api('status');
    if(!Array.isArray(result.data?.groups)||!result.data?.schema)throw new Error('插件状态不完整，请重载 ACG 群友后重新打开控制台。');
    state=result.data;
    renderImageBudget(state.image_test_budget);
    $('version').textContent=state.version;
    const selected=$('group').value;
    $('group').replaceChildren(...state.groups.map(g=>{const o=node('option',g.name);o.value=g.umo;return o;}));
    if(state.groups.some(g=>g.umo===selected))$('group').value=selected;
    const imageSelected=$('image-group').value;
    $('image-group').replaceChildren(...state.groups.map(g=>{const o=node('option',g.name);o.value=g.umo;return o;}));
    if(state.groups.some(g=>g.umo===imageSelected))$('image-group').value=imageSelected;
    if(config||!inputs.size){revision=state.revision;renderConfig();}
    renderGroup();
    if(!imageJob&&state.image_test){imageJob=state.image_test.token;imageRunning=state.image_test.status==='running';void pollImage();}
  }
  function renderGroup() {
    const g=group();
    if(!g){$('budget').textContent='请到插件设置中添加目标群';$('blockers').textContent='尚未配置目标群。';return;}
    $('budget').textContent=`今日自动发送 ${g.count}/${g.cap} · 手动试发 ${g.manual_count??0}/${g.manual_cap??10} · 本群模型 ${g.calls}/${g.call_cap} · 全部群模型 ${state.total_calls}/${state.total_call_cap}`;
    $('pause').textContent=g.paused?'恢复本群':'暂停本群';
    $('blockers').replaceChildren(...(g.blockers.length?g.blockers:['本地条件已满足，等待到期任务和模型判断']).map(x=>node('span',x,'chip')));
    $('schedule').replaceChildren(...g.plan.map(x=>node('span',fmt(x.due)+(x.expires<Date.now()/1000?' · 已过期':''),'chip')));
    if(!g.plan.length)$('schedule').textContent='今天没有随机分享计划。';
    $('pending').replaceChildren(...g.pending.filter(x=>x.kind!=='share').map(x=>{const n=node('div',`${labels[x.kind]||x.kind} · ${x.text}`,'entry');n.append(node('small',`${x.name||'群友'} · ${fmt(x.due)} 起可跟进`));return n;}));
    if(!$('pending').childNodes.length)$('pending').textContent='暂无待跟进话题。';
    $('history').replaceChildren(...g.attempts.map(x=>{const n=node('div',x.text,'entry');n.prepend(node('small',`${fmt(x.ts)} · ${labels[x.kind]||x.kind} · ${states[x.status]||x.status}`));return n;}));
    if(!g.attempts.length)$('history').textContent='暂无发送记录；网页预览不计为已发送。';
    updateTopics();
  }
  function updateTopics() {
    const selected=$('topic').value, kind=$('kind').value;
    $('topic').replaceChildren(node('option','自己填写模拟场景（仅预览）'));
    $('topic').firstChild.value='';
    for(const topic of group()?.pending||[])if(topic.kind===kind){const o=node('option',`${topic.name||'群友'}：${topic.text.slice(0,50)}`);o.value=topic.id;$('topic').append(o);}
    if([...$('topic').options].some(o=>o.value===selected))$('topic').value=selected;
    updateMode();
  }
  function updateMode() {
    const kind=$('kind').value, real=Boolean($('topic').value), share=kind==='share';
    $('source-label').hidden=!share;
    $('topic-label').hidden=share||kind==='thursday';
    $('scene-label').hidden=share||real||kind==='thursday';
    $('mode-hint').textContent=share?'读取真实热榜和当前群人格，生成短评；不会自动发群消息。':real?'使用本群已记录的真实话题和后续聊天判断；未到原定时间也可手动预览。':'模拟场景只用于网页测试语气，可以修改原话；不会写入群聊记录，也不能直接发到群里。';
  }
  function renderConfig() {
    inputs=new Map();keyClears=new Map();$('config-fields').replaceChildren();
    const sections=[
      ['常用设置',['enabled','sessions','provider_id','daily_limit','quiet_hours','share_enabled','followups_enabled','banter_enabled','image_enabled']],
      ['生图接口',['image_api_url','image_api_key','image_model']],
      ['高级：频率与额度',['idle_minutes','min_interval_minutes','other_bot_gap_minutes','ignored_user_ids','daily_model_calls','global_daily_model_calls','manual_send_daily_limit','image_test_daily_limit','image_test_interval_seconds','media_interval_days','style_prompt','image_api_type'],true],
      ['实验：歌曲生成',['music_enabled','music_api_url','music_api_key','music_model'],true]
    ];
    for(const [title,keys,collapsed]of sections){const section=node(collapsed?'details':'div',undefined,'config-section');section.append(node(collapsed?'summary':'h3',title));const grid=node('div',undefined,'field-grid');
      for(const key of keys){const spec=state.schema[key];if(!spec)continue;const label=node('label');let input;
        if(spec.type==='bool'){input=node('input');input.type='checkbox';input.checked=Boolean(state.config[key]);label.append(input,document.createTextNode(spec.description));}
        else if(spec.options){label.append(document.createTextNode(spec.description));input=node('select');for(const value of spec.options){const option=node('option',value);option.value=value;input.append(option);}input.value=state.config[key]??spec.default;label.append(input);}
        else {label.append(document.createTextNode(spec.description));input=node(spec.type==='list'||spec.type==='text'?'textarea':'input');if(input.tagName==='TEXTAREA'){input.rows=spec.type==='list'?2:3;label.className='wide';}
          if(spec.type==='int'){input.type='number';input.min=spec.min;input.max=spec.max;input.step='1';}else if(input.tagName==='INPUT')input.type=key.endsWith('_api_key')?'password':'text';
          input.value=spec.type==='list'?(state.config[key]||[]).join('\n'):(state.config[key]??'');
          if(key.endsWith('_api_key')){input.autocomplete='new-password';input.placeholder=state.secrets[key]?'已配置，留空保留':'尚未配置';}
          label.append(input);
        }
        if(spec.hint)label.append(node('span',spec.hint,'hint'));inputs.set(key,input);
        if(key.endsWith('_api_key')){const clear=node('span',undefined,'clear-key'),cb=node('input');cb.type='checkbox';clear.append(cb,document.createTextNode('清除已保存的密钥'));label.append(clear);keyClears.set(key,cb);}
        grid.append(label);
      }section.append(grid);$('config-fields').append(section);
    }
  }
  $('refresh').onclick=()=>connected?action(async()=>{await load();notice('状态已刷新；未保存的设置保留在表单中。');}):connect();
  $('group').onchange=()=>{invalidate();renderGroup();};
  $('kind').onchange=()=>{invalidate();$('scene').value=samples[$('kind').value]||'';updateTopics();};
  $('source').onchange=invalidate;
  $('image-group').onchange=renderManualBudget;
  $('topic').onchange=()=>{invalidate();updateMode();};
  $('scene').oninput=invalidate;
  $('generate').onclick=()=>action(async()=>{
    invalidate();notice('正在取内容并生成预览…');
    const result=await api('preview',{umo:$('group').value,kind:$('kind').value,source:$('source').value,topic_id:$('topic').value,text:$('scene').value,request_id:crypto.randomUUID()});
    draft=result.data;
    if(draft.decision==='skip'){$('preview-text').textContent='这次它决定先不说。';$('preview-note').textContent=draft.reason;}
    else{$('preview-text').textContent=draft.text;$('preview-text').hidden=false;renderCard(draft.card);
      const format=draft.card?'封面图片＋标题＋原站链接。':'';
      $('preview-note').textContent=draft.simulation?'模拟预览，不会发送到群里。':`${format}预览将在 ${fmt(draft.expires)} 过期；可以点击下方按钮发送。`;}
    $('preview-meta').textContent=(draft.source?draft.source+' · ':'')+(draft.simulation?'语气演练':`群 ${draft.umo.split(':').pop()}`);
    await load();notice(draft.decision==='skip'?'生成完成：模型选择跳过。':'预览已生成，尚未发到群里。');
  });
  async function sendPreview(current, noteId, image=false) {
    if(!current?.sendable)return;
    current.sendable=false;
    const pending=image?'正在发送这张图到预览目标群…':'正在发送这条预览…';
    $(noteId).textContent=pending;notice(pending);
    let result;
    try {result=await api('send',{token:current.token});}
    catch(e){const message=(e.message||'发送未完成')+(image?'；可点击“检查生图进度”核对预览状态，本页不会自动重发。':'；请核对状态，本页不会自动重发。');$(noteId).textContent=message;notice(message,true);return;}
    const message=result.message||(result.data?.result==='sent'?(image?'已发送到预览目标群':'已发送到选定群'):'发送结果不确定，请到群里确认；不会自动重发。');
    $(noteId).textContent=message;notice(message,result.data?.result!=='sent');
    try {await load();}catch(e){notice(message+'；状态刷新失败，可稍后手动刷新。',true);}
  }
  $('send').onclick=()=>action(()=>sendPreview(draft,'preview-note'));
  async function pollImage() {
    clearTimeout(imageTimer);
    if(!imageJob)return;
    const current=imageJob;
    try {
      const result=await api('image-status',{token:current});
      if(current!==imageJob)return;
      const info=result.data;
      renderImageBudget(info.budget);
      imageRunning=info.status==='running';
      if(imageRunning){$('image-note').textContent=(info.stage||'正在等待生图服务…')+'；只查询进度，不会重复提交。';imageTimer=setTimeout(pollImage,2000);}
      else if(info.status==='ready'&&info.preview){imageDraft=info.preview;
        $('image-preview').src=imageDraft.image;$('image-preview').hidden=false;$('image-text').textContent=imageDraft.text;$('image-text').hidden=false;
        $('image-meta').textContent=`${imageDraft.title} · 目标群 ${imageDraft.umo.split(':').pop()}`;
        $('image-note').textContent=(imageDraft.reused?'复用已有图片，本次未重新调用生图服务。':'生成完成，尚未发到群里。')+` 预览在 ${fmt(imageDraft.expires)} 过期。`+(imageDraft.caption_note?' '+imageDraft.caption_note:'');
      } else {$('image-note').textContent=(info.message||'生图未完成；不会自动重新提交。')+(imageDraft?' 上一张预览已保留。':'');}
    } catch(e){imageRunning=false;$('image-note').textContent=(e.message||'读取进度失败')+'；可点击“检查生图进度”继续读取，本页不会重新提交。'+(imageDraft?' 上一张预览已保留。':'');}
    finally{syncControls();}
  }
  $('image-generate').onclick=()=>action(async()=>{
    notice('正在提交生图测试…');
    const result=await api('image-start',{umo:$('image-group').value,prompt:$('image-prompt').value,caption:$('image-caption').value,fresh:Boolean($('image-fresh').checked),request_id:crypto.randomUUID()});
    imageJob=result.data.token;imageRunning=true;$('image-fresh').checked=false;
    notice('生图测试已开始，只在网页预览。');await pollImage();
  });
  $('image-check').onclick=()=>action(pollImage);
  $('image-rewrite').onclick=()=>action(async()=>{
    if(!imageDraft?.sendable)return;
    const current=imageDraft;
    $('image-note').textContent='正在根据图片主题重写配文，图片不会重新生成…';
    try {
      const result=await api('image-caption',{token:current.token,theme:$('image-prompt').value,request_id:crypto.randomUUID()});
      current.text=result.data.text;current.caption_origin='model';current.caption_note='';$('image-text').textContent=current.text;$('image-caption').value='';
      $('image-note').textContent='配文已重写，图片未重新生成；发送将使用新配文。';notice('配文已更新，尚未发送。');
    }catch(e){$('image-note').textContent=(e.message||'配文重写未完成')+'；原图片和配文已保留。';notice(e.message||'配文重写未完成',true);}
  });
  $('image-send').onclick=()=>action(()=>sendPreview(imageDraft,'image-note',true));
  $('pause').onclick=()=>action(async()=>{const g=group();if(!g)return;const paused=!g.paused;const result=await api('pause',{umo:g.umo,paused});await load();notice(result.message||(paused?'已暂停':'已恢复'));});
  $('settings-form').onsubmit=event=>{event.preventDefault();action(async()=>{
    const values={};for(const [key,input]of inputs){const spec=state.schema[key];values[key]=spec.type==='bool'?input.checked:spec.type==='int'?Number(input.value):spec.type==='list'?input.value.split('\n').map(x=>x.trim()).filter(Boolean):input.value;}
    const clear_secrets=[...keyClears].filter(([,cb])=>cb.checked).map(([key])=>key);
    const result=await api('config',{values,clear_secrets,revision});invalidate();invalidateImage();await load(true);notice(result.message||'已保存并立即生效');
  });};
  document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{document.querySelectorAll('.tab-content').forEach(s=>s.hidden=s.id!==button.dataset.tab);document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('selected',b===button));});
  syncControls();
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',connect,{once:true});
  else connect();
})();
