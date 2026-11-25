'use strict';
const $ = id => document.getElementById(id);
let ready = false, polling = false, submitting = false, checkingConnection = false;
let pendingTaskAction = null, recoveryBusy = false, recoveryGeneration = 0;
let recoveries = [], discardCandidate = null;
let catalog = [], engineValues = {}, currentState = null, lastStatus = '', historyFingerprint = '', queueFingerprint = '', fileIdentity = '';
let preferenceTimer, validationTimer, toastTimer, validationSequence = 0, localError = '', windowMaximized = false, windowFloating = false, windowOnTop = false, switchingWindow = false, lastMainFocus = null;
const credentialStates=new Map();let credentialViewSequence=0,credentialMutation=false;
const translations = {zh:'简体中文',en:'英语','zh-tw':'繁体中文',ja:'日语',de:'德语',fr:'法语',ko:'韩语',es:'西班牙语'};
const working = state => state && ['preparing','running','pausing','cancelling'].includes(state.status);
const selectedEngine = () => catalog.find(engine => engine.id === $('engine').value);
const iconMarkup = name => '<svg aria-hidden="true"><use href="#' + name + '"></use></svg>';
const queueFiles = state => Array.isArray(state?.files) ? state.files : state?.file ? [state.file] : [];
function toast(message) { $('toast').textContent=message; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,4500); }
function showError(message) { localError=message||''; $('error-message').textContent=localError; $('error-message').hidden=!localError; }
async function invoke(method,...args) {
  if(!window.pywebview?.api?.[method])return {ok:false,error:'本地服务尚未就绪，请稍后重试。'};
  try{return await window.pywebview.api[method](...args);}catch(_){return {ok:false,error:'操作未完成，请重试。'};}
}
function applyTheme(theme) {
  const dark=theme==='dark'; document.documentElement.dataset.theme=dark?'dark':'light';
  $('theme-label').textContent=dark?'亮色模式':'暗色模式';
  $('theme-icon').querySelector('use').setAttribute('href',dark?'#sun':'#moon');
  $('theme-toggle').setAttribute('aria-label',dark?'切换到亮色模式':'切换到暗色模式');
  $('theme-toggle').title=dark?'切换到亮色模式':'切换到暗色模式';
}
function applyWindowState(state) {
  if(!state||typeof state.maximized!=='boolean')return;
  const wasFloating=windowFloating;
  windowMaximized=state.maximized;
  if(typeof state.floating==='boolean')windowFloating=state.floating;
  if(typeof state.always_on_top==='boolean')windowOnTop=state.always_on_top;
  if(windowFloating&&!wasFloating){
    lastMainFocus=document.activeElement;
    if($('settings-dialog').open)closeSettings();
  }
  document.documentElement.dataset.maximized=String(windowMaximized);
  document.documentElement.dataset.windowMode=windowFloating?'floating':'main';
  syncAppInert();
  $('floating-panel').hidden=!windowFloating;
  $('window-maximize').querySelector('use').setAttribute('href',windowMaximized?'#restore':'#maximize');
  $('window-maximize').setAttribute('aria-label',windowMaximized?'还原窗口':'最大化');
  $('window-maximize').title=windowMaximized?'还原窗口':'最大化';
  $('window-floating').querySelector('use').setAttribute('href',windowFloating?'#expand':'#floating');
  $('window-floating').setAttribute('aria-label',windowFloating?'返回主窗口':'切换到悬浮窗');
  $('window-floating').title=windowFloating?'返回主窗口':'切换到悬浮窗';
  $('floating-pin').setAttribute('aria-pressed',String(windowOnTop));
  $('floating-pin').setAttribute('aria-label',windowOnTop?'取消置顶':'置顶悬浮窗');
  $('floating-pin').title=windowOnTop?'取消置顶':'置顶悬浮窗';
  $('floating-pin').querySelector('use').setAttribute('href',windowOnTop?'#pin':'#unpin');
  if(currentState)renderFloating(currentState);
  if(windowFloating&&!wasFloating)$('floating-restore').focus({preventScroll:true});
  else if(!windowFloating&&wasFloating){
    const focus=lastMainFocus&&lastMainFocus.isConnected&&lastMainFocus.offsetParent!==null&&!lastMainFocus.disabled?lastMainFocus:$('window-floating');
    focus.focus({preventScroll:true});lastMainFocus=null;
  }
}
async function changeWindowMode(restore=false){
  if(!ready)return;
  if(credentialMutation){toast('正在保存密钥，请稍候。');return;}
  if(switchingWindow)return;
  switchingWindow=true;$('window-floating').disabled=true;$('floating-restore').disabled=true;
  try{const result=await invoke(restore?'window_exit_floating':'window_toggle_floating');if(result?.ok===false)toast(result.error||'暂时无法切换窗口模式。');else applyWindowState(result);}finally{switchingWindow=false;$('window-floating').disabled=!ready;$('floating-restore').disabled=false;}
}
async function toggleMaximize(){if(windowFloating){await changeWindowMode(true);return;}applyWindowState(await invoke('window_toggle_maximize'));}
$('window-floating').onclick=()=>changeWindowMode(windowFloating);
$('floating-restore').onclick=()=>changeWindowMode(true);
$('floating-pin').onclick=async()=>{
  $('floating-pin').disabled=true;
  try{const result=await invoke('window_set_floating_on_top',!windowOnTop);if(result?.ok===false)toast(result.error||'暂时无法调整置顶。');else applyWindowState(result);}finally{$('floating-pin').disabled=false;}
};
$('window-minimize').onclick=()=>invoke('window_minimize');
$('window-maximize').onclick=toggleMaximize;
$('window-close').onclick=()=>invoke('window_close');
$('titlebar-drag').onmousedown=event=>{if(event.button===0&&event.detail===1)invoke('window_start_drag');};
$('titlebar-drag').ondblclick=toggleMaximize;
document.querySelectorAll('[data-resize]').forEach(handle=>handle.addEventListener('pointerdown',event=>{if(event.button===0&&!windowMaximized){event.preventDefault();invoke('window_start_resize',handle.dataset.resize);}}));
window.addEventListener('resize',()=>{if(ready)invoke('window_get_state').then(applyWindowState);});

function renderFloating(state){
  const files=queueFiles(state),batch=state.batch||{},busy=working(state),status=state.status||'idle';
  const total=batch.total||files.length,complete=batch.completed??files.filter(file=>file.status==='done').length;
  const failed=batch.failed??files.filter(file=>file.status==='error').length,cancelled=batch.cancelled??files.filter(file=>file.status==='cancelled').length;
  const active=files.find(file=>file.id===state.file?.id)||files.find(file=>file.status==='running')||state.file||files[0];
  const percent=Math.max(0,Math.min(100,Math.floor(batch.progress??state.progress??0)));
  const labels={paused:'进度已保存',pausing:'正在暂停并保存',cancelling:'正在取消',idle:files.length?'等待开始':'等待翻译',preparing:'准备翻译',running:total>1?'正在翻译 '+Math.max(1,batch.current_index||1)+' / '+total+' 份':'正在翻译',done:'队列已完成',partial:'部分文档未完成',error:'翻译未完成',cancelled:'队列已取消'};
  $('floating-status').textContent=labels[status]||'等待翻译';
  $('floating-panel').dataset.active=String(busy);
  $('floating-file').textContent=active?.name||'尚未选择文档';$('floating-file').title=active?.name||'';
  $('floating-count').textContent=complete+' / '+total+' 已完成';
  $('floating-percent').textContent=percent+'%';$('floating-fill').style.width=percent+'%';$('floating-progress').setAttribute('aria-valuenow',String(percent));
  let detail=status==='idle'?(files.length?'返回主窗口开始翻译':'返回主窗口选择 PDF'):status==='done'?'所有结果已保存':status==='partial'?failed+' 份未完成，返回主窗口查看':status==='error'?'返回主窗口查看错误原因':status==='cancelled'?(cancelled?cancelled+' 份已取消，已完成结果保留':'已完成结果保留'):state.stage||'正在处理文档';
  if(status==='paused')detail='可以关闭 ORCA，返回主窗口可继续。';
  if(status==='pausing')detail='请等待保存完成。';
  if(status==='running'&&(state.activity?.seconds_since_update||0)>20)detail='仍在处理中，请稍候';
  $('floating-stage').textContent=detail;$('floating-stage').title=detail;
  $('floating-elapsed').textContent=elapsedText(state.elapsed);
  $('floating-cancel').hidden=!busy;
  $('floating-output').hidden=busy||!complete;
  if(!busy){for(const id of ['cancel-button','floating-cancel']){$(id).disabled=false;$(id).textContent='取消翻译';}}
}

function readEngineFields() {
  const engine=selectedEngine();if(!engine)return {};
  const values={...(engineValues[engine.id]||{})};
  for(const field of engine.fields||[]){const input=$('setting-'+field.name);if(input)values[field.name]=field.type==='checkbox'?input.checked:input.value;}
  engineValues[engine.id]=values;return values;
}
const credentialFields = engine => (engine?.fields||[]).filter(field=>field.type==='password');
const isEndpointField = field => field.type!=='password'&&/(?:url|host|endpoint)$/i.test(field.name);
function credentialState(engineId){
  if(!credentialStates.has(engineId))credentialStates.set(engineId,{loaded:false,loading:false,remembered:false,choice:false,choiceDirty:false,token:0,loadView:0,promise:null,endpoint:'',autofilled:new Map(),edited:new Set(),message:'',error:false});
  return credentialStates.get(engineId);
}
function endpointSignature(engine,values){return JSON.stringify((engine?.fields||[]).filter(isEndpointField).map(field=>[field.name,String(values[field.name]??field.default??'').trim()]));}
function credentialMessage(message,error=false,engineId=$('engine').value){const meta=credentialState(engineId);meta.message=message||'';meta.error=error;if($('engine').value===engineId)renderCredentialControls();}
function renderCredentialControls(){
  const engine=selectedEngine(),fields=credentialFields(engine),meta=credentialState(engine?.id||'');
  $('credential-storage').hidden=!fields.length;$('remember-credentials').checked=meta.choice;
  $('remember-credentials').disabled=credentialMutation;
  $('forget-credentials').disabled=credentialMutation||meta.loading||(!meta.remembered&&!fields.some(field=>$('setting-'+field.name)?.value));
  $('credential-feedback').textContent=meta.loading?'正在读取已保存的密钥…':meta.message;
  $('credential-feedback').hidden=!fields.length||(!meta.loading&&!meta.message);$('credential-feedback').dataset.error=String(meta.error&&!meta.loading);
  $('apply-settings').disabled=credentialMutation||meta.loading;
}
function notifyCredentialChange(){readEngineFields();$('engine-fields').dispatchEvent(new Event('input',{bubbles:true}));}
function editCredentialField(field){
  const engine=selectedEngine();if(!engine)return;const meta=credentialState(engine.id);
  if(field.type==='password'){meta.edited.add(field.name);meta.autofilled.delete(field.name);meta.message=meta.choice?'点击“使用此引擎”后保存新密钥。':'当前密钥仅在本次打开软件期间使用。';meta.error=false;}
  if(isEndpointField(field)){
    meta.token++;meta.loading=false;meta.promise=null;meta.loaded=false;
    let cleared=false;
    for(const [name,value]of meta.autofilled){const input=$('setting-'+name);if(input?.value===value){input.value='';if(engineValues[engine.id])engineValues[engine.id][name]='';cleared=true;}}
    meta.autofilled.clear();meta.message=cleared?'接口地址已更改，已清除自动填入的密钥。请填写新接口对应的密钥。':'接口地址已更改，请确认密钥与该接口匹配。';meta.error=false;
  }
  renderCredentialControls();
}
function loadCredentials(){
  const engine=selectedEngine();if(!engine||!credentialFields(engine).length)return Promise.resolve({ok:true});
  const meta=credentialState(engine.id),values=readEngineFields(),endpoint=endpointSignature(engine,values),view=credentialViewSequence;
  if(meta.loaded&&meta.endpoint===endpoint)return Promise.resolve({ok:true});
  if(meta.loading&&meta.loadView===view&&meta.promise)return meta.promise;
  const token=++meta.token;meta.loading=true;meta.loadView=view;renderCredentialControls();
  meta.promise=(async()=>{
    const result=await invoke('load_engine_credentials',engine.id,values);
    if(token!==meta.token||view!==credentialViewSequence||$('engine').value!==engine.id){if(token===meta.token){meta.loading=false;meta.loaded=false;}return {ok:false,cancelled:true};}
    meta.loading=false;
    if(!result?.ok){meta.loaded=false;credentialMessage(result?.error||'无法读取已保存的密钥，请重试。',true,engine.id);return {ok:false};}
    meta.loaded=true;meta.endpoint=endpoint;meta.remembered=result.remembered===true;if(!meta.choiceDirty)meta.choice=meta.remembered;
    let changed=false;
    for(const field of credentialFields(engine)){
      const input=$('setting-'+field.name),value=result.secrets?.[field.name];
      if(!result.endpoint_mismatch&&typeof value==='string'&&value&&!meta.edited.has(field.name)&&!input?.value){input.value=value;engineValues[engine.id][field.name]=value;meta.autofilled.set(field.name,value);changed=true;}
    }
    meta.message=result.endpoint_mismatch?'已保存的密钥属于其他接口地址，未自动填入。请填写当前接口对应的密钥。':meta.remembered?'此服务的密钥已保存在 Windows 凭据管理器中。':'';meta.error=false;
    renderCredentialControls();if(changed)notifyCredentialChange();return {ok:true};
  })();return meta.promise;
}
function credentialBusy(enabled){
  credentialMutation=enabled;
  for(const input of document.querySelectorAll('#engine,#engine-fields input,#engine-fields select,#close-settings,#engine-test-button'))input.disabled=enabled;
  $('apply-settings').innerHTML=enabled?'正在应用…':'使用此引擎'+iconMarkup('arrow');renderCredentialControls();
}
async function applyEngineSettings(){
  if(credentialMutation)return;credentialBusy(true);let applied=false;
  try{
    const engine=selectedEngine();if(!engine)return;
    if(credentialFields(engine).length){
      const loaded=await loadCredentials();if(!loaded?.ok){
        const meta=credentialState(engine.id);
        if(!meta.choice&&!meta.remembered){applied=true;toast('已使用本次密钥；已保存的密钥暂时无法读取。');}
        return;
      }
      const meta=credentialState(engine.id),values=readEngineFields();let result;
      if(meta.choice){result=await invoke('save_engine_credentials',engine.id,values);if(!result?.ok||result.remembered!==true){credentialMessage(result?.error||'密钥未能保存，请重试。',true);return;}meta.remembered=true;meta.message='密钥已保存在 Windows 凭据管理器中。';}
      else if(meta.remembered){result=await invoke('forget_engine_credentials',engine.id);if(!result?.ok||result.remembered!==false){credentialMessage(result?.error||'已保存的密钥未能删除，请重试。',true);return;}meta.remembered=false;meta.message='已删除保存的密钥，当前密钥仅本次使用。';}
      meta.choiceDirty=false;meta.error=false;
    }
    applied=true;
  }finally{credentialBusy(false);if(applied)closeSettings();}
}
async function forgetCredentials(){
  if(credentialMutation)return;const engine=selectedEngine();if(!engine)return;const meta=credentialState(engine.id);meta.token++;meta.loading=false;credentialBusy(true);
  try{
    const result=await invoke('forget_engine_credentials',engine.id);
    if(!result?.ok||result.remembered!==false){credentialMessage(result?.error||'密钥未能删除，请重试。',true);return;}
    meta.loaded=true;meta.endpoint=endpointSignature(engine,readEngineFields());meta.remembered=false;meta.choice=false;meta.choiceDirty=false;meta.autofilled.clear();
    for(const field of credentialFields(engine)){meta.edited.add(field.name);const input=$('setting-'+field.name);if(input)input.value='';engineValues[engine.id][field.name]='';}
    notifyCredentialChange();credentialMessage('已忘记此服务的密钥，本次输入也已清空。');
  }finally{credentialBusy(false);}
}
function safePreferences() {
  readEngineFields();const profiles={};
  for(const engine of catalog){const values=engineValues[engine.id]||{},safe={};for(const field of engine.fields||[])if(field.type!=='password'&&!/(api.?key|secret|token|password|authorization)/i.test(field.name)&&values[field.name]!==undefined)safe[field.name]=values[field.name];profiles[engine.id]=safe;}
  return {theme:document.documentElement.dataset.theme,lang_in:$('lang-in').value,lang_out:$('lang-out').value,output:document.querySelector('input[name="output"]:checked').value,engine:$('engine').value,page_mode:$('page-mode').value,engine_settings:profiles};
}
function savePreferencesSoon(){if(!ready)return;clearTimeout(preferenceTimer);preferenceTimer=setTimeout(async()=>{const result=await invoke('save_preferences',safePreferences());if(result?.ok===false)toast(result.error||'设置暂未保存。');},350);}
function options(mode){return {auto_save:$('auto-save').checked,translate_images:$('engine').value==='indextranslate'&&$('translate-images').checked,engine:$('engine').value,engine_settings:readEngineFields(),lang_in:$('lang-in').value,lang_out:$('lang-out').value,page_mode:mode||$('page-mode').value,pages:$('pages').value.trim(),output:document.querySelector('input[name="output"]:checked').value};}
function fieldElement(field,values){
  const label=document.createElement('label');label.className=field.type==='checkbox'?'checkbox-field':'field';
  const input=document.createElement('input');input.id='setting-'+field.name;input.dataset.field=field.name;input.type=field.type==='checkbox'?'checkbox':field.type==='password'?'password':'text';input.autocomplete='off';input.spellcheck=false;
  const value=values[field.name]!==undefined?values[field.name]:field.default;
  if(field.type==='checkbox')input.checked=value===true;else input.value=value===null||value===undefined?'':String(value);
  input.placeholder=field.type==='password'?'输入密钥':field.placeholder||'';
  const title=document.createElement('span');title.textContent=field.label||field.name;
  if(field.type==='password'){const badge=document.createElement('small');badge.className='secret-badge';badge.textContent='密钥';title.append(badge);}
  if(field.type==='checkbox')label.append(input,title);else label.append(title,input);
  input.addEventListener('input',()=>{editCredentialField(field);readEngineFields();$('connection-feedback').hidden=true;savePreferencesSoon();});return label;
}
function renderEngineFields(){
  credentialViewSequence++;
  const engine=selectedEngine(),container=$('engine-fields');container.replaceChildren();$('engine-description').textContent=engine?.description||'';$('connection-feedback').hidden=true;
  const fields=engine?.fields||[],values=engineValues[engine?.id]||{};
  if(!fields.length){const empty=document.createElement('div');empty.className='engine-empty';empty.textContent='无需密钥。选择服务后即可开始翻译，使用时需要联网。';container.append(empty);}
  for(const field of fields.filter(f=>!f.advanced))container.append(fieldElement(field,values));
  const advanced=fields.filter(f=>f.advanced);if(advanced.length){const details=document.createElement('details');details.className='engine-advanced';const summary=document.createElement('summary');summary.textContent='高级参数';const contents=document.createElement('div');for(const field of advanced)contents.append(fieldElement(field,values));details.append(summary,contents);container.append(details);}
  readEngineFields();$('settings-button').title=engine?.label||'引擎设置';renderCredentialControls();void loadCredentials();
}
function populateEngines(engines,preferences){
  catalog=engines||[];engineValues=JSON.parse(JSON.stringify(preferences.engine_settings||{}));$('engine').replaceChildren();const groups=new Map();
  for(const engine of catalog){const category=engine.category||'翻译服务';if(!groups.has(category)){const group=document.createElement('optgroup');group.label=category;groups.set(category,group);$('engine').append(group);}const option=document.createElement('option');option.value=engine.id;option.textContent=engine.label;groups.get(category).append(option);}
  $('engine').value=catalog.some(e=>e.id===preferences.engine)?preferences.engine:catalog.find(e=>e.id==='bing')?.id||catalog[0]?.id||'';renderEngineFields();
}
function openSettings(){if(windowFloating||working(currentState)||!ready)return;$('settings-overlay').hidden=false;$('settings-dialog').show();$('engine').focus();void loadCredentials();}
function closeSettings(){if(credentialMutation)return;readEngineFields();savePreferencesSoon();$('settings-dialog').close();$('settings-overlay').hidden=true;scheduleValidation();$('settings-button').focus();}
function updateRangeUi(){$('range-field').hidden=$('page-mode').value!=='custom';updateButtons();scheduleValidation();}
function updateButtons(){
  const isBusy=working(currentState)||submitting||recoveryBusy,files=queueFiles(currentState),hasFiles=files.length>0;
  $('translate-button').disabled=!ready||!hasFiles||isBusy;$('trial-button').disabled=!ready||!hasFiles||isBusy;
  const caption=currentState?.status==='paused'?'重新开始翻译':isBusy?'翻译进行中…':$('page-mode').value==='all'?(files.length>1?'翻译全部 '+files.length+' 份':'翻译全文'):'翻译所选页';
  $('translate-button').innerHTML=caption+(isBusy?'':iconMarkup('arrow'));
  $('trial-button').textContent=files.length>1?'每份试译 5 页':'试译 '+Math.min(5,files[0]?.pages||5)+' 页';
}
function localRangeSummary(){const files=queueFiles(currentState);if(!files.length)return '选中文档后显示翻译页数';if($('page-mode').value==='all')return files.length+' 份文档 · 共 '+files.reduce((sum,file)=>sum+(file.pages||0),0)+' 页';return '指定页码将应用到每份文档';}
function scheduleValidation(){clearTimeout(validationTimer);validationTimer=setTimeout(validateRange,280);}
async function validateRange(){
  const sequence=++validationSequence;$('pages-feedback').textContent=localRangeSummary();$('pages-feedback').classList.remove('invalid');
  if(!ready||!queueFiles(currentState).length||working(currentState))return;
  const result=await invoke('validate_options',options());if(sequence!==validationSequence)return;
  if(result?.ok){$('pages-feedback').textContent=(result.pages_label||'所选范围')+' · 共 '+result.page_count+' 页';$('pages').removeAttribute('aria-invalid');}
  else if(['pages','page_mode','range'].includes(result?.field)||$('page-mode').value==='custom'&&result?.error){$('pages-feedback').textContent=result.error;$('pages-feedback').classList.add('invalid');$('pages').setAttribute('aria-invalid','true');}
}
function updateJobOptions(busy) {
  const canTranslateImages = selectedEngine()?.id === 'indextranslate';
  $('auto-save').disabled = busy;
  $('translate-images').disabled = busy || !canTranslateImages;
  if (!canTranslateImages) $('translate-images').checked = false;
  $('image-option-note').textContent = canTranslateImages
    ? '开启后，图片会发送至 B站，图中文字的译文附在输出 PDF 末尾。'
    : '使用 Index-Translate 可翻译图片文字。';
}

function renderTaskActions(state) {
  const busy = working(state);
  const saving = state.status === 'pausing' || pendingTaskAction === 'pause';
  const cancelling = state.status === 'cancelling' || pendingTaskAction === 'cancel';
  for (const id of ['pause-button', 'floating-pause']) {
    $(id).hidden = !busy;
    $(id).disabled = saving || cancelling;
    $(id).textContent = saving ? '正在保存…' : '暂停并保存';
  }
  for (const id of ['cancel-button','floating-cancel']) {
    $(id).hidden = !busy;
    $(id).disabled = saving || cancelling;
    $(id).textContent = cancelling ? '正在取消…' : '取消翻译';
  }
  for (const button of $('recovery-list').querySelectorAll('button')) {
    button.disabled = busy || recoveryBusy || submitting;
  }
}

async function requestTaskAction(action) {
  if (!working(currentState) || pendingTaskAction) return;
  pendingTaskAction = action;
  renderTaskActions(currentState);
  let accepted = false;
  try {
    const result = await invoke(action === 'pause' ? 'pause_and_save' : 'cancel');
    if (!result?.ok) {
      toast(result?.error || '未能完成操作，请重试。');
      return;
    }
    accepted = true;
    // An accepted pause is not yet a durable save; wait for the worker's state.
    if (action === 'pause') toast('正在保存进度，请等待“已暂停”提示。');
    await poll();
  } finally {
    if (!accepted || !working(currentState)) pendingTaskAction = null;
    if (currentState) renderTaskActions(currentState);
  }
}

function recoveryMessage(message) {
  const node = $('recovery-message');
  node.textContent = message;
  node.hidden = !message;
}

function renderRecoveries() {
  const container = $('recovery-list');
  container.replaceChildren();
  $('recovery-section').hidden = !recoveries.length && $('recovery-message').hidden;
  for (const item of recoveries) {
    const row = document.createElement('div');
    row.className = 'recovery-row';
    const info = document.createElement('div');
    const title = document.createElement('strong');
    title.textContent = item.file_name || '翻译任务';
    const details = document.createElement('small');
    details.textContent = [item.engine_label || item.engine,
      '已保存 ' + item.saved_pages + ' / ' + item.total_pages + ' 页',
      item.saved_segments ? item.saved_segments + ' 段译文' : '',
      item.updated_at ? dateText(item.updated_at) : ''].filter(Boolean).join(' · ');
    info.append(title,details);
    const resume = document.createElement('button');
    resume.className = 'secondary-button';
    resume.textContent = '继续翻译';
    resume.onclick = () => resumeRecovery(item);
    const discard = document.createElement('button');
    discard.className = 'text-button';
    discard.textContent = '删除进度';
    discard.onclick = () => {
      discardCandidate = item.id;
      $('discard-recovery-description').textContent = '删除“' + title.textContent + '”的续译记录和已保存的中间进度后，将无法从此处继续。';
      $('discard-recovery-dialog').showModal();
      $('keep-recovery').focus();
    };
    row.append(info,resume,discard);
    container.append(row);
  }
  if (currentState) renderTaskActions(currentState);
}

async function refreshRecoveries() {
  if (!ready) return;
  const sequence = ++recoveryGeneration;
  const result = await invoke('list_recoveries');
  if (sequence !== recoveryGeneration) return;
  if (result?.ok && Array.isArray(result.recoveries)) {
    recoveries = result.recoveries;
    recoveryMessage('');
  } else {
    recoveryMessage(result?.error || '未能读取保存的进度，请刷新。');
  }
  renderRecoveries();
}

async function resumeRecovery(item) {
  if (recoveryBusy || working(currentState) || submitting) return;
  recoveryBusy = true;
  renderTaskActions(currentState);
  recoveryMessage('');
  try {
    const engine = selectedEngine();
    // The backend restores the job's own options, and looks up bound vault
    // keys. Send session credentials only when the visible provider matches.
    const supplied = engine?.id === item.engine
      ? { engine: engine.id, engine_settings: readEngineFields() } : {};
    const result = await invoke('resume_saved',item.id,supplied);
    if (!result?.ok) {
      recoveryMessage(result?.error || '无法继续翻译，请检查保存的任务。');
      if (result?.field === 'source' && result.file_id) {
        const locate = document.createElement('button');
        locate.className = 'text-button';
        locate.textContent = '重新选择原文件';
        locate.onclick = async () => {
          if (working(currentState) || recoveryBusy) return;
          recoveryBusy = true;
          locate.disabled = true;
          renderTaskActions(currentState);
          try {
            const selected = await invoke('relocate_saved',item.id,result.file_id);
            if (selected?.ok) recoveryMessage('已确认原文件，可继续翻译。');
            else if (!selected?.cancelled) {
              $('recovery-message').firstChild.textContent = selected?.error || '未能确认原文件，请重新选择。';
            }
          } finally {
            locate.disabled = false;
            recoveryBusy = false;
            renderTaskActions(currentState);
          }
        };
        $('recovery-message').append(' ',locate);
      } else if (result?.field === 'engine') {
        const configure = document.createElement('button');
        configure.className = 'text-button';
        configure.textContent = '配置此服务';
        configure.onclick = () => {
          if (!catalog.some(entry=>entry.id===item.engine) || working(currentState)) return;
          $('engine').value = item.engine;
          $('engine').dispatchEvent(new Event('change'));
          openSettings();
        };
        $('recovery-message').append(' ',configure);
      }
      return;
    }
    await poll();
    await refreshRecoveries();
    $('progress-card').scrollIntoView({block:'nearest'});
  } finally {
    recoveryBusy = false;
    if (currentState) renderTaskActions(currentState);
  }
}

$('recovery-refresh').onclick = refreshRecoveries;
$('discard-recovery-dialog').addEventListener('close', () => {discardCandidate = null;});
$('keep-recovery').onclick = () => $('discard-recovery-dialog').close();
$('confirm-discard-recovery').onclick = async () => {
  const id = discardCandidate;
  $('discard-recovery-dialog').close();
  if (!id || working(currentState) || recoveryBusy || submitting) return;
  recoveryBusy = true;
  renderTaskActions(currentState);
  try {
    const result = await invoke('discard_saved',id);
    if (!result?.ok) recoveryMessage(result?.error || '删除保存进度失败。');
    else await refreshRecoveries();
  } finally {
    recoveryBusy = false;
    if (currentState) renderTaskActions(currentState);
  }
};

function formatBytes(value){return ((value||0)/1048576).toFixed(2)+' MB';}
function renderQueue(files,isBusy){
  const fingerprint=JSON.stringify(files)+'|'+isBusy;if(fingerprint===queueFingerprint)return;queueFingerprint=fingerprint;
  $('file-list').replaceChildren();$('file-list').hidden=!files.length;$('file-count').hidden=!files.length;$('file-count').textContent=files.length+' 份';$('clear-files').hidden=!files.length;$('clear-files').disabled=isBusy;
  for(const file of files){
    const row=document.createElement('div');row.className='queue-row'+(file.status==='running'?' is-running':'');row.dataset.fileId=file.id||file.path;row.innerHTML='<svg class="queue-icon" aria-hidden="true"><use href="#document"/></svg>';
    const info=document.createElement('div');info.className='queue-info';const name=document.createElement('strong');name.textContent=file.name;name.title=file.path||file.name;const meta=document.createElement('small');meta.textContent=(file.pages||0)+' 页 · '+formatBytes(file.bytes)+(file.has_text===false?' · 可能为扫描版':'');info.append(name,meta);
    if(file.error){const error=document.createElement('small');error.className='queue-error';error.textContent=file.error;info.append(error);}
    const actions=document.createElement('div');actions.className='queue-actions';const status=document.createElement('span');status.className='queue-status'+(file.status==='error'?' is-error':'');status.textContent=({paused:'已暂停',pausing:'正在保存',ready:'待翻译',queued:'等待中',running:'处理中 '+Math.floor(file.progress||0)+'%',done:'已完成',error:'未完成',cancelled:'已取消'})[file.status]||'待翻译';actions.append(status);
    for(const [kind,title]of[['dual','双语 ↗'],['mono','译文 ↗']])if(file.results?.[kind]){const button=document.createElement('button');button.className='queue-result';button.textContent=title;button.setAttribute('aria-label','打开 '+file.name+' 的'+(kind==='dual'?'双语文档':'译文'));button.onclick=async()=>{const result=await invoke('open_queue_result',file.id,kind);if(result?.error)toast(result.error);};actions.append(button);}
    if(!isBusy){const remove=document.createElement('button');remove.className='queue-remove';remove.innerHTML=iconMarkup('close');remove.title='从队列移除';remove.setAttribute('aria-label','移除 '+file.name);remove.onclick=async()=>{const result=await invoke('remove_file',file.id);if(result?.error)showError(result.error);else showError(null);await poll();};actions.append(remove);}
    row.append(info,actions);$('file-list').append(row);
  }
}
function dateText(value){const date=new Date(typeof value==='number'&&value<1e12?value*1000:value);return Number.isNaN(date.getTime())?'':date.toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'})+' '+date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});}
function renderHistory(history){
  const items=(history||[]).slice(0,20),fingerprint=JSON.stringify(items);if(fingerprint===historyFingerprint)return;historyFingerprint=fingerprint;$('history-count').textContent=items.length?String(items.length):'';
  if(!items.length){$('history-list').innerHTML='<div class="history-empty">'+iconMarkup('clock')+'<p>翻译过的文档会保留在这里<span>下次打开 ORCA，仍可快速找到结果。</span></p></div>';return;}
  $('history-list').replaceChildren();for(const item of items){const row=document.createElement('div');row.className='history-row';row.innerHTML=iconMarkup('document');const info=document.createElement('div');info.className='history-info';const name=document.createElement('strong');name.textContent=item.file_name||'PDF 文档';name.title=name.textContent;const meta=document.createElement('small');meta.textContent=[dateText(item.created_at),item.engine_label,item.pages_label].filter(Boolean).join(' · ');info.append(name,meta);const actions=document.createElement('div');actions.className='history-actions';const status=document.createElement('span');status.className='history-status';status.textContent=({done:'已完成',cancelled:'已取消',error:'未完成',running:'处理中'})[item.status]||item.status;actions.append(status);
    for(const [kind,title]of[['dual','双语 ↗'],['mono','译文 ↗']])if(item.results?.[kind]){const button=document.createElement('button');button.className='text-button';button.textContent=title;button.onclick=async()=>{const result=await invoke('open_history_result',item.id,kind);if(result?.error)toast(result.error);};actions.append(button);}
    const again=document.createElement('button');again.className='text-button';again.textContent='再次翻译';again.dataset.historySelect='true';again.disabled=working(currentState);again.onclick=async()=>{const result=await invoke('select_history',item.id);if(result?.ok){showError(null);await poll();$('workspace').scrollIntoView({behavior:'smooth'});}else if(result?.error)toast(result.error);};actions.append(again);row.append(info,actions);$('history-list').append(row);}
}
function updateStages(state){let step=0;if(/翻译正文|重试|网络/.test(state.stage||''))step=1;if(/排版|字体|页面|保存|结果|翻译完成/.test(state.stage||''))step=2;document.querySelectorAll('.stage-steps li').forEach((item,index)=>{item.classList.toggle('active',index===step&&working(state));item.classList.toggle('complete',index<step&&working(state)||state.status==='done');});}
function elapsedText(seconds){seconds=Math.max(0,Math.floor(seconds||0));return String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');}
function render(state){
  if (!working(state)) pendingTaskAction = null;
  currentState=state;const isBusy=working(state),files=queueFiles(state),batch=state.batch||{},file=state.file,task=state.task||{};const identity=files.map(item=>item.id||item.path).join('|');
  updateJobOptions(isBusy);
  $('workspace').classList.toggle('is-working',isBusy);$('file-button').classList.toggle('has-files',files.length>0);
  $('file-name').textContent=files.length?'已选择 '+files.length+' 份 PDF':'选择 PDF 文件';$('file-name').title=files.length===1?files[0].name:'';
  $('file-meta').textContent=files.length?'共 '+files.reduce((sum,item)=>sum+(item.pages||0),0)+' 页 · '+formatBytes(files.reduce((sum,item)=>sum+(item.bytes||0),0)):'可一次选择多份论文、书籍或报告';
  $('choose-label').innerHTML=(files.length?'添加文件':'选择文件')+iconMarkup('plus');$('scan-note').hidden=!files.some(item=>item.has_text===false);
  renderQueue(files,isBusy);$('file-button').disabled=isBusy;$('folder-button').disabled=isBusy;$('settings-button').disabled=isBusy;$('cancel-button').hidden=!isBusy;$('trial-button').hidden=isBusy;
  for(const id of ['lang-in','lang-out','page-mode','pages'])$(id).disabled=isBusy;document.querySelectorAll('input[name="output"]').forEach(input=>input.disabled=isBusy);document.querySelectorAll('[data-history-select]').forEach(button=>button.disabled=isBusy);
  $('output-dir').textContent=state.output_dir||'';$('output-dir').title=state.output_dir||'';updateButtons();
  $('progress-card').hidden=state.status==='idle';const totals=batch.total||files.length;const done=batch.completed??files.filter(item=>item.status==='done').length;const failed=batch.failed??files.filter(item=>item.status==='error').length;const cancelled=batch.cancelled??files.filter(item=>item.status==='cancelled').length;
  const percent=Math.max(0,Math.min(100,Math.floor(batch.progress??state.progress??0)));$('percentage').textContent=percent+'%';$('progress-fill').style.width=percent+'%';$('batch-progress').setAttribute('aria-valuenow',String(percent));
  $('stage').textContent=state.status==='paused'?'已暂停，进度已保存':state.status==='pausing'?'正在暂停并保存':state.status==='cancelling'?'正在取消':state.status==='done'?'队列翻译完成':state.status==='partial'?'队列已结束，部分未完成':state.status==='cancelled'?'队列已取消':state.status==='error'?'队列未完成':totals>1?'正在翻译 '+(batch.current_index||1)+' / '+totals+' 份':state.stage||'准备翻译';
  $('task-badge').textContent=task.trial?'每份试译':'文档翻译';$('task-summary').textContent=[task.engine_label,task.lang_in?(translations[task.lang_in]||task.lang_in)+' → '+(translations[task.lang_out]||task.lang_out):null,task.pages_label].filter(Boolean).join(' · ');
  $('current-document').hidden=!isBusy||!file;$('current-document-name').textContent=file?.name||'';$('file-progress').textContent=(state.stage||'处理中')+' · '+Math.floor(state.progress||0)+'%';
  $('part').textContent=state.part||'';$('elapsed').textContent=elapsedText(state.elapsed);$('progress-dot').className='progress-dot'+(state.status==='done'?' done':['partial','error','cancelled'].includes(state.status)?' error':'');updateStages(state);
  $('progress-description').textContent=state.status==='paused'?'可以安全关闭 ORCA。下次打开后在“可继续的翻译”中继续。':state.status==='pausing'?'正在保存翻译进度，请等待保存完成后再关闭。':state.status==='done'?'所有结果已保存，可在文件列表逐份打开。':state.status==='partial'?'已保留完成的文件，请检查未完成项的提示。':state.status==='cancelled'?'已停止后续翻译，已完成的结果仍可打开。':state.status==='error'?'请检查文件列表中的提示后重试。':state.status==='preparing'?'准备字体和版面识别模型，首次使用可能稍慢。':'按队列顺序逐份处理，完成后自动开始下一份。';
  const lastUpdate=state.activity?.last_update_at;if(state.status==='running'&&lastUpdate){const stamp=typeof lastUpdate==='number'?lastUpdate*1000:Date.parse(lastUpdate);if(Date.now()-stamp>20000)$('progress-description').textContent='当前步骤仍在处理中；翻译服务响应可能较慢。';}
  const error=state.error||localError;$('error-message').textContent=error||'';$('error-message').hidden=!error;
  $('result-actions').hidden=isBusy||state.status==='idle';$('batch-summary').textContent=[done+' 份已完成',failed?failed+' 份未完成':null,cancelled?cancelled+' 份已取消':null].filter(Boolean).join(' · ');$('continue-full').hidden=!task.trial||state.status!=='done';
  $('action-hint').textContent=isBusy?'完成的结果可从列表中打开。':files.length>1?'使用相同设置，按顺序逐份翻译。':'不确定效果？先试译几页。';
  if(Array.isArray(state.history))renderHistory(state.history);if(state.status!==lastStatus){if(['done','partial','cancelled','error'].includes(state.status))refreshHistory();if(state.status==='done'&&lastStatus)toast('队列翻译完成，文件已保存。');if (lastStatus !== null && !working(state)) void refreshRecoveries();
    $('cancel-button').disabled=false;lastStatus=state.status;}
  if(identity!==fileIdentity){fileIdentity=identity;scheduleValidation();}
  renderFloating(state);
  renderTaskActions(state);
}
async function poll(){if(!ready||polling)return;polling=true;try{const [state,nativeState]=await Promise.all([invoke('get_state'),invoke('window_get_state')]);if(state?.status)render(state);applyWindowState(nativeState);}finally{polling=false;}}
async function refreshHistory(){const data=await invoke('get_bootstrap');if(data?.history)renderHistory(data.history);}
async function start(mode){
  if(!ready||working(currentState)||submitting||recoveryBusy)return;submitting=true;updateButtons();showError(null);const request=options(mode);
  try{const valid=await invoke('validate_options',request);if(!valid?.ok){showError(valid?.error||'请检查翻译设置。');if(valid?.field==='engine')openSettings();return;}await invoke('save_preferences',safePreferences());const result=await invoke('start_translation',request);if(result?.ok){$('auto-save').checked=false;$('translate-images').checked=false;$('selection-notice').hidden=true;await poll();$('progress-card').scrollIntoView({behavior:'smooth',block:'nearest'});}else{showError(result?.error||'未能开始翻译。');if(result?.field==='engine')openSettings();}}finally{submitting=false;updateButtons();}
}
$('theme-toggle').onclick=()=>{applyTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');savePreferencesSoon();};
$('file-button').onclick=async()=>{const result=await invoke('pick_file');if(result?.error)showError(result.error);else if(result?.ok)showError(null);const rejected=result?.rejected||[];$('selection-notice').textContent=rejected.length?'未加入 '+rejected.length+' 份文件：'+rejected.map(item=>(item.name||'文件')+'（'+(item.error||'无法读取')+'）').join('；'):'';$('selection-notice').hidden=!rejected.length;await poll();};
$('clear-files').onclick=async()=>{const result=await invoke('clear_files');if(result?.error)showError(result.error);else{showError(null);$('selection-notice').hidden=true;}await poll();};
$('folder-button').onclick=async()=>{const result=await invoke('pick_output');if(result?.error)toast(result.error);await poll();};
$('settings-button').onclick=openSettings;$('close-settings').onclick=closeSettings;$('settings-overlay').onclick=closeSettings;
$('settings-form').addEventListener('submit',event=>{event.preventDefault();void applyEngineSettings();});
$('remember-credentials').onchange=()=>{const meta=credentialState($('engine').value);meta.choice=$('remember-credentials').checked;meta.choiceDirty=true;credentialMessage(meta.choice?'点击“使用此引擎”后，密钥会保存在当前电脑上。':meta.remembered?'点击“使用此引擎”后删除已保存的密钥，本次仍可继续使用。':'当前密钥仅在本次打开软件期间使用。');};
$('forget-credentials').onclick=forgetCredentials;
$('settings-dialog').addEventListener('keydown',event=>{
  if(event.key==='Escape'){event.preventDefault();closeSettings();return;}
  if(event.key==='Tab'){const list=Array.from($('settings-dialog').querySelectorAll('button,input,select,summary,[tabindex]')).filter(item=>!item.disabled&&item.offsetParent!==null&&item.tabIndex>=0);const first=list[0],last=list.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}
});
$('engine').addEventListener('change',()=>{updateJobOptions(working(currentState));renderEngineFields();savePreferencesSoon();scheduleValidation();});
$('engine-test-button').onclick=async()=>{if(checkingConnection)return;checkingConnection=true;$('engine-test-button').disabled=true;$('engine-test-button').textContent='正在测试…';$('connection-feedback').hidden=false;$('connection-feedback').textContent='正在向所选服务发送测试文本…';const engineId=$('engine').value;try{const result=await invoke('test_engine',options());if(engineId===$('engine').value)$('connection-feedback').textContent=result?.ok?(result.message||'连接成功，可以使用此引擎。'):(result?.error||'连接失败，请检查服务地址和凭据。');}finally{checkingConnection=false;$('engine-test-button').disabled=false;$('engine-test-button').textContent='测试连接';}};
$('page-mode').onchange=()=>{updateRangeUi();savePreferencesSoon();};$('pages').addEventListener('input',scheduleValidation);for(const id of ['lang-in','lang-out'])$(id).onchange=()=>{savePreferencesSoon();scheduleValidation();};document.querySelectorAll('input[name="output"]').forEach(input=>input.onchange=savePreferencesSoon);
$('translate-button').onclick=()=>start();$('trial-button').onclick=()=>start('first5');$('continue-full').onclick=()=>{$('page-mode').value='all';updateRangeUi();start('all');};
$('cancel-button').onclick=()=>requestTaskAction('cancel');$('floating-cancel').onclick=()=>requestTaskAction('cancel');
$('pause-button').onclick=()=>requestTaskAction('pause');$('floating-pause').onclick=()=>requestTaskAction('pause');
$('floating-output').onclick=()=>invoke('open_output');
$('open-output').onclick=()=>invoke('open_output');$('source-button').onclick=()=>invoke('open_source');$('history-refresh').onclick=refreshHistory;
let connectStarted=false,startupPolling=false,frontendBootstrapping=false,startupFailed=false;
let startupGeneration=0,startupPollTimer=null,appPollTimer=null,backendStartupStatus='waiting';
window.__orcaReady=false;window.__orcaStartupVisible=true;

function syncAppInert(){
  for(const node of [document.querySelector('main'),document.querySelector('.sidebar'),$('reader-view'),$('settings-dialog'),$('settings-overlay')])if(node)node.inert=!ready||windowFloating;
  $('floating-panel').inert=!ready||!windowFloating;
  $('window-floating').disabled=!ready||switchingWindow;
}
function showStartupStage(message){
  $('startup-overlay').hidden=false;window.__orcaStartupVisible=true;document.documentElement.dataset.startup='loading';
  $('startup-card').dataset.error='false';$('startup-card').setAttribute('aria-busy','true');
  $('startup-title').textContent='正在启动 ORCA';$('startup-stage').textContent=message||'正在准备…';
  $('startup-error').hidden=true;$('startup-actions').hidden=true;syncAppInert();
}
function showStartupError(message){
  startupFailed=true;frontendBootstrapping=false;clearTimeout(startupPollTimer);startupPollTimer=null;
  document.documentElement.dataset.startup='error';
  $('startup-card').dataset.error='true';$('startup-card').setAttribute('aria-busy','false');
  $('startup-title').textContent='启动遇到问题';$('startup-stage').textContent='可以重试，或退出后重新打开。';
  $('startup-error').textContent=message||'本地服务暂时无法准备完成，请重试。';$('startup-error').hidden=false;
  $('startup-actions').hidden=false;$('startup-retry').disabled=false;syncAppInert();
}
function withStartupTimeout(promise,timeout=15000){
  let timer;return Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('本地服务响应超时，请重试。')),timeout);})]).finally(()=>clearTimeout(timer));
}
async function startupInvoke(method,...args){return await withStartupTimeout(invoke(method,...args));}
function scheduleStartupPoll(){
  if(ready||startupFailed||frontendBootstrapping)return;
  clearTimeout(startupPollTimer);startupPollTimer=setTimeout(pollStartup,250);
}
async function bootstrapWorkspace(generation){
  if(frontendBootstrapping||ready)return;frontendBootstrapping=true;
  try{
    showStartupStage('正在载入界面设置…');
    const data=await startupInvoke('get_bootstrap');if(generation!==startupGeneration)return;
    if(!Array.isArray(data?.engines)||!data.engines.length)throw new Error(data?.error||'无法读取本地设置，请重试。');
    // A failed frontend attempt may have left a credential read in flight.
    // Invalidate it before rebuilding fields, rather than overwriting new input.
    credentialViewSequence++;for(const meta of credentialStates.values())meta.token++;credentialStates.clear();
    const prefs=data.preferences||{};applyTheme(prefs.theme||'light');populateEngines(data.engines,prefs);
    for(const [id,key]of[['lang-in','lang_in'],['lang-out','lang_out'],['page-mode','page_mode']])if(prefs[key]&&Array.from($(id).options).some(option=>option.value===prefs[key]))$(id).value=prefs[key];
    const output=Array.from(document.querySelectorAll('input[name="output"]')).find(input=>input.value===prefs.output);if(output)output.checked=true;
    if(credentialFields(selectedEngine()).length){
      showStartupStage('正在读取已保存的密钥…');
      try{await withStartupTimeout(loadCredentials(),12000);}catch(_){
        credentialViewSequence++;for(const meta of credentialStates.values()){meta.token++;meta.loading=false;meta.promise=null;meta.loaded=false;}
        credentialMessage('已保存的密钥暂时无法读取，可以在引擎设置中重新填写。',true);
      }
      if(generation!==startupGeneration)return;
    }
    showStartupStage('正在准备工作区…');
    const [state,nativeState]=await Promise.all([startupInvoke('get_state'),startupInvoke('window_get_state')]);
    if(generation!==startupGeneration)return;
    if(!state?.status)throw new Error(state?.error||'无法读取工作区状态，请重试。');
    $('version').textContent='ORCA '+(data.version||'0.9.1');renderHistory(data.history||[]);render(state);applyWindowState(nativeState);
    ready=true;startupFailed=false;updateRangeUi();syncAppInert();
    document.documentElement.dataset.startup='ready';$('startup-card').setAttribute('aria-busy','false');$('startup-overlay').hidden=true;
    window.__orcaStartupVisible=false;window.__orcaReady=true;
    void refreshRecoveries();
    if(window.pywebview?.api?.startup_ui_ready)requestAnimationFrame(()=>{void invoke('startup_ui_ready');});
    clearTimeout(startupPollTimer);startupPollTimer=null;if(!appPollTimer)appPollTimer=setInterval(poll,800);
  }catch(error){if(generation===startupGeneration)showStartupError(error?.message||'工作区未能准备完成，请重试。');}
  finally{if(generation===startupGeneration)frontendBootstrapping=false;}
}
async function pollStartup(){
  if(startupPolling||ready||startupFailed||frontendBootstrapping)return;
  startupPolling=true;const generation=startupGeneration;
  try{
    const state=await startupInvoke('get_startup_state');if(generation!==startupGeneration)return;
    if(!state||!['waiting','loading','ready','error','closed'].includes(state.status))throw new Error(state?.error||'无法读取启动状态，请重试。');
    backendStartupStatus=state.status;if(state.theme==='dark'||state.theme==='light')applyTheme(state.theme);
    if(state.status==='error'){showStartupError(state.error||'启动未能完成，请重试。');return;}
    if(state.status==='closed'){showStartupStage('正在关闭 ORCA…');return;}
    if(state.status==='ready'){await bootstrapWorkspace(generation);return;}
    showStartupStage(state.stage||'正在准备翻译功能…');
  }catch(error){if(generation===startupGeneration)showStartupError(error?.message||'本地服务暂时无法连接，请重试。');}
  finally{startupPolling=false;if(generation===startupGeneration&&backendStartupStatus!=='closed')scheduleStartupPoll();}
}
async function connect(){
  if(connectStarted||ready)return;connectStarted=true;startupFailed=false;showStartupStage('正在连接本地服务…');await pollStartup();
}
async function retryStartup(){
  if($('startup-retry').disabled||ready)return;
  $('startup-retry').disabled=true;startupGeneration++;startupFailed=false;frontendBootstrapping=false;
  clearTimeout(startupPollTimer);startupPollTimer=null;
  showStartupStage('正在重新准备…');
  try{
    if(backendStartupStatus==='error'){
      const result=await startupInvoke('retry_startup');
      if(result?.ok===false)throw new Error(result.error||'暂时无法重试，请稍后再试。');
    }
    connectStarted=false;await connect();
  }catch(error){showStartupError(error?.message||'暂时无法重试，请稍后再试。');}
}
$('startup-retry').onclick=retryStartup;$('startup-exit').onclick=()=>invoke('window_close');
syncAppInert();
window.addEventListener('pywebviewready',connect);if(window.pywebview?.api)connect();
setTimeout(()=>{if(!connectStarted&&!ready)showStartupError('窗口已打开，但本地服务尚未连接。请重试或退出后重新打开。');},30000);

// The reader shares session engine settings; credential persistence is explicit.
window.orcaReaderHost={invoke,options,toast,isReady:()=>ready,engineLabel:()=>selectedEngine()?.label||'未选择引擎',savePreferences:savePreferencesSoon,engineId:()=>selectedEngine()?.id,isBatchBusy:()=>working(currentState)};
