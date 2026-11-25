/* Continuous pages retain their geometry; only nearby pages keep live canvases. */
const el = id => document.getElementById(id);
const host = window.orcaReaderHost;
const state = {document:null,pdf:null,loading:null,page:1,scale:1,fit:true,rendering:false,selection:'',translation:'',status:'idle',metrics:null};
let documentGeneration=0,requestSequence=0,activeRequest=0;
let selectionTimer=null,resizeTimer=null,warmTimer=null,pdfLibrary=null,picking=false,closing=false;
let pageRecords=[],referenceWidth=612,referenceHeight=792,scrollFrame=0,pinnedPages=new Set();
const renderingTasks=new Set(),liveRecords=new Set(),waitingRecords=new Set(),MAX_RENDERED=5,MAX_PARALLEL=2,PAGE_GAP=24;
let selectingOnPage=false;
let imageMode = false, imageDrag = null, selectedImage = null;
const limit=6000;

function setStatus(message,status='idle'){
  state.status=status;el('reader-status').textContent=message;el('reader-status').dataset.state=status;
  el('reader-cancel').hidden=status!=='loading';
  el('reader-translate').disabled=!state.selection||state.selection.length>limit||status==='loading'||!host.isReady();
  el('reader-translate').textContent=status==='error'?'重试翻译':state.translation?'重新翻译':selectedImage?'翻译所选图片':'翻译选中文字';
  el('reader-copy').disabled=!state.translation;
}
function cancelRequest(){
  clearTimeout(selectionTimer);requestSequence++;
  if(activeRequest){const previous=activeRequest;activeRequest=0;void host.invoke('reader_cancel',previous);}
}
function clearSelection(){
  stopImageMode();
  selectedImage = null;
  cancelRequest();state.selection='';state.translation='';state.metrics=null;el('reader-selection').textContent='在左侧页面选中需要翻译的文字。';el('reader-selection').classList.add('reader-placeholder');el('reader-selection-count').textContent='';el('reader-translation').textContent='';setStatus('等待划选');
}
function pageStatus(message){el('reader-page-status').textContent=message;el('reader-page-status').hidden=!message;}
function updateControls(){
  const loaded=!!state.pdf,total=state.pdf?.numPages||0;
  el('reader-prev').disabled=!loaded||state.page<=1;el('reader-next').disabled=!loaded||state.page>=total;
  el('reader-page-number').disabled=!loaded;if(document.activeElement!==el('reader-page-number'))el('reader-page-number').value=state.page;el('reader-page-number').max=total||1;
  el('reader-page-total').textContent='/ '+(total||'—');
  for(const id of ['reader-zoom-in','reader-zoom-out','reader-fit'])el(id).disabled=!loaded;
  el('reader-close').hidden=!state.document;
  updateImageTool();
  el('reader-zoom-label').textContent=Math.round(state.scale*100)+'%';
}
function syncSettings(){
  updateImageTool();
  for(const suffix of ['in','out']){
    const original=el('lang-'+suffix),target=el('reader-lang-'+suffix);
    if(!target.options.length)for(const option of original.options)target.append(option.cloneNode(true));
    target.value=original.value;target.disabled=host.isBatchBusy();
  }
  el('reader-engine').textContent=host.engineLabel();el('reader-engine').title=host.engineLabel();
  el('reader-theme').querySelector('use').setAttribute('href',document.documentElement.dataset.theme==='dark'?'#moon':'#sun');
}
function showView(view){
  const reading=view==='reader';document.querySelector('main').hidden=reading;el('reader-view').hidden=!reading;
  document.documentElement.dataset.activeView=reading?'reader':'workspace';
  for(const id of ['nav-workspace','nav-history','nav-reader'])el(id).classList.toggle('active',id===(reading?'nav-reader':view==='history'?'nav-history':'nav-workspace'));
  if(reading){syncSettings();if(state.pdf&&state.fit)queueFit();}
  else{stopImageMode();cancelRequest();if(state.status==='loading')setStatus('翻译已取消，返回后可重新翻译。','cancelled');if(view==='history')el('history-section').scrollIntoView({behavior:'smooth'});}
}
async function library(){
  if(!pdfLibrary)pdfLibrary=import('./vendor/pdfjs/build/pdf.mjs').then(module=>{module.GlobalWorkerOptions.workerSrc=new URL('./vendor/pdfjs/build/pdf.worker.mjs',import.meta.url).href;return module;}).catch(error=>{pdfLibrary=null;throw error;});
  return pdfLibrary;
}
function pageAt(y){
  let low=0,high=pageRecords.length-1;
  while(low<high){const middle=Math.floor((low+high+1)/2);if(pageRecords[middle].top<=y)low=middle;else high=middle-1;}
  return pageRecords[low];
}
function scrollAnchor(){const viewport=el('reader-viewport'),stack=el('reader-pages'),y=viewport.scrollTop-stack.offsetTop,record=pageAt(Math.max(0,y));return record?{page:record.number,ratio:(y-record.top)/record.height}:null;}
function reflow(anchor=null){
  const viewport=el('reader-viewport'),stack=el('reader-pages');let top=0,maxWidth=0;
  for(const record of pageRecords){record.width=record.naturalWidth*state.scale;record.height=record.naturalHeight*state.scale;record.top=top;record.element.style.width=record.width+'px';record.element.style.height=record.height+'px';record.element.style.top=top+'px';record.element.style.setProperty('--scale-factor',String(state.scale));record.element.style.setProperty('--total-scale-factor',String(state.scale));top+=record.height+PAGE_GAP;maxWidth=Math.max(maxWidth,record.width);}
  stack.style.height=Math.max(0,top-PAGE_GAP)+'px';stack.style.width=maxWidth+'px';
  if(anchor){const record=pageRecords[anchor.page-1];if(record)viewport.scrollTop=Math.max(0,stack.offsetTop+record.top+anchor.ratio*record.height);}
}
function pagePlaceholder(record,message){record.element.replaceChildren();const label=document.createElement('span');label.className='reader-page-placeholder';label.textContent=message||'第 '+record.number+' 页';record.element.append(label);record.element.dataset.rendered='false';}
function settlePage(record,value){waitingRecords.delete(record);for(const resolve of record.waiters.splice(0))resolve(value);}
function evictPage(record){
  record.token++;try{record.renderTask?.cancel();}catch(_){}try{record.textTask?.cancel();}catch(_){}
  record.renderTask=null;record.textTask=null;
  for(const canvas of record.element.querySelectorAll('canvas')){canvas.width=0;canvas.height=0;}
  if(record.status==='ready')try{record.pageProxy?.cleanup();}catch(_){}
  liveRecords.delete(record);record.pageProxy=null;record.status='empty';record.element.onclick=null;pagePlaceholder(record);settlePage(record,false);
}
function stopPages(){cancelAnimationFrame(scrollFrame);scrollFrame=0;for(const record of pageRecords)evictPage(record);pageRecords=[];pinnedPages.clear();state.rendering=false;el('reader-pages').replaceChildren();}
async function disposePdf(){
  stopPages();const oldLoading=state.loading,oldPdf=state.pdf;state.loading=null;state.pdf=null;
  try{if(oldLoading)await oldLoading.destroy();else if(oldPdf)await oldPdf.destroy();}catch(_){}
}
async function openDocument(documentInfo){
  if(!documentInfo?.id||!documentInfo?.url)return false;
  const generation=++documentGeneration;clearSelection();await disposePdf();if(generation!==documentGeneration)return false;
  state.document=documentInfo;state.page=1;state.fit=true;prepareReader(0);
  showView('reader');el('reader-empty').hidden=true;el('reader-pages').hidden=true;el('reader-document-name').textContent=documentInfo.name;el('reader-document-name').title=documentInfo.name;pageStatus('正在打开文档…');updateControls();
  try{
    const pdfjs=await library();if(generation!==documentGeneration)return false;
    const loading=pdfjs.getDocument({url:documentInfo.url,disableStream:true,disableAutoFetch:true,rangeChunkSize:65536,isEvalSupported:false,cMapUrl:new URL('./vendor/pdfjs/cmaps/',import.meta.url).href,cMapPacked:true,standardFontDataUrl:new URL('./vendor/pdfjs/standard_fonts/',import.meta.url).href,wasmUrl:new URL('./vendor/pdfjs/wasm/',import.meta.url).href,enableXfa:false});
    state.loading=loading;const pdf=await loading.promise;if(generation!==documentGeneration){await pdf.destroy();return false;}
    state.pdf=pdf;const firstPage=await pdf.getPage(1);if(generation!==documentGeneration)return false;
    const initial=firstPage.getViewport({scale:1});referenceWidth=initial.width;referenceHeight=initial.height;
    state.scale=fitScale();const fragment=document.createDocumentFragment();
    for(let number=1;number<=pdf.numPages;number++){
      const element=document.createElement('div');element.id='reader-page-'+number;element.className='reader-page';element.dataset.pageNumber=number;element.setAttribute('aria-label','PDF 第 '+number+' 页');
      const record={number,element,naturalWidth:referenceWidth,naturalHeight:referenceHeight,width:0,height:0,top:0,status:'empty',token:0,waiters:[],pageProxy:null,renderTask:null,textTask:null};pageRecords.push(record);pagePlaceholder(record);fragment.append(element);
    }
    el('reader-pages').replaceChildren(fragment);el('reader-pages').hidden=false;pageStatus('');reflow();el('reader-viewport').scrollTop=0;updateControls();return await goToPage(1);
  }catch(error){if(generation===documentGeneration){pageStatus('文档未能打开，请关闭后重试或选择其他 PDF。');setStatus('文档未能打开。','error');updateControls();}return false;}
}
async function pickDocument(){
  if(picking||closing||!host.isReady())return;picking=true;el('reader-open').disabled=true;el('reader-empty-open').disabled=true;
  try{const result=await host.invoke('reader_pick_file');if(result?.ok)await openDocument(result.document);else if(!result?.cancelled)pageStatus(result?.error||'无法打开这份 PDF。');}
  finally{picking=false;el('reader-open').disabled=false;el('reader-empty-open').disabled=false;}
}
async function closeDocument(){
  if(closing)return;closing=true;el('reader-open').disabled=true;el('reader-empty-open').disabled=true;
  ++documentGeneration;clearSelection();state.document=null;const closed=host.invoke('reader_close_document');await disposePdf();state.page=1;state.scale=1;
  el('reader-pages').hidden=true;el('reader-empty').hidden=false;el('reader-document-name').textContent='划选一段文字，专注读懂这一段。';el('reader-document-name').title='';pageStatus('');updateControls();
  await closed;closing=false;el('reader-open').disabled=false;el('reader-empty-open').disabled=false;
}
async function renderRecord(record){
  const token=++record.token,generation=documentGeneration,pdf=state.pdf,taskKey={};record.status='loading';renderingTasks.add(taskKey);liveRecords.add(record);pagePlaceholder(record,'正在载入第 '+record.number+' 页…');
  const current=()=>generation===documentGeneration&&token===record.token;let page=null;
  try{
    page=await pdf.getPage(record.number);if(!current())return;
    const natural=page.getViewport({scale:1});
    if(record.naturalWidth!==natural.width||record.naturalHeight!==natural.height){const anchor=scrollAnchor();record.naturalWidth=natural.width;record.naturalHeight=natural.height;reflow(anchor);}
    const viewport=page.getViewport({scale:state.scale});const ratio=Math.min(window.devicePixelRatio||1,2);
    const canvas=document.createElement('canvas');canvas.setAttribute('aria-label','PDF 第 '+record.number+' 页');canvas.width=Math.ceil(viewport.width*ratio);canvas.height=Math.ceil(viewport.height*ratio);canvas.style.width=viewport.width+'px';canvas.style.height=viewport.height+'px';
    const textLayer=document.createElement('div');textLayer.id='reader-text-layer-'+record.number;textLayer.className='textLayer';
    const pdfjs=await library();if(!current())return;
    record.element.replaceChildren(canvas,textLayer);record.pageProxy=page;
    record.renderTask=page.render({canvasContext:canvas.getContext('2d'),viewport,transform:ratio===1?null:[ratio,0,0,ratio,0,0]});
    const canvasFinished=record.renderTask.promise;canvasFinished.catch(()=>{});
    const content=await page.getTextContent();if(!current())return;
    record.textTask=new pdfjs.TextLayer({textContentSource:content,container:textLayer,viewport});
    await Promise.all([canvasFinished,record.textTask.render()]);if(!current())return;
    record.renderTask=null;record.textTask=null;record.status='ready';record.element.dataset.rendered='true';
    if(!content.items.some(item=>item.str?.trim())){const note=document.createElement('span');note.className='reader-scan-badge';note.textContent='此页没有可选文字';record.element.append(note);}
    settlePage(record,true);
  }catch(error){if(current()){record.status='error';pagePlaceholder(record,'第 '+record.number+' 页未能显示，点击重试');record.element.onclick=()=>{record.status='empty';record.element.onclick=null;scheduleViewport();};settlePage(record,false);}}
  finally{renderingTasks.delete(taskKey);if(!current())try{page?.cleanup();}catch(_){}scheduleViewport();}
}
function updateViewport(){
  scrollFrame=0;if(!state.pdf||!pageRecords.length||el('reader-view').hidden||document.documentElement.dataset.windowMode==='floating')return;
  const viewport=el('reader-viewport'),y=viewport.scrollTop-el('reader-pages').offsetTop;
  state.page=pageAt(Math.max(0,y+Math.min(120,viewport.clientHeight*.2))).number;
  const visible=[];for(let index=pageAt(Math.max(0,y)).number-1;index<pageRecords.length&&pageRecords[index].top<=y+viewport.clientHeight;index++)visible.push(pageRecords[index]);
  const priority=[state.page,...visible.map(record=>record.number)];
  for(let distance=1;priority.length<MAX_RENDERED*2&&distance<=3;distance++)priority.push(state.page+distance,state.page-distance);
  const desired=new Set(priority.filter(number=>number>0&&number<=pageRecords.length).filter((value,index,list)=>list.indexOf(value)===index).slice(0,MAX_RENDERED));
  for(const number of pinnedPages)desired.add(number);
  for(const record of liveRecords)if(!desired.has(record.number))evictPage(record);
  for(const record of waitingRecords)if(!desired.has(record.number))settlePage(record,false);
  for(const number of desired){if(renderingTasks.size>=MAX_PARALLEL)break;const record=pageRecords[number-1];if(record.status==='empty')void renderRecord(record);}
  state.rendering=pageRecords[state.page-1]?.status!=='ready';updateControls();
}
function scheduleViewport(){if(!scrollFrame)scrollFrame=requestAnimationFrame(updateViewport);}
function fitScale(){return Math.min(3,Math.max(.25,(el('reader-viewport').clientWidth-(innerWidth<=1050?38:50))/referenceWidth));}
async function goToPage(number){
  stopImageMode();
  if(!state.pdf||!pageRecords.length)return false;number=Math.max(1,Math.min(pageRecords.length,Math.floor(Number(number)||1)));
  const record=pageRecords[number-1];state.page=number;el('reader-page-number').value=number;
  el('reader-viewport').scrollTop=Math.max(0,el('reader-pages').offsetTop+record.top-8);updateControls();updateViewport();
  if(record.status==='ready')return true;if(record.status==='error')return false;
  return await new Promise(resolve=>{record.waiters.push(resolve);waitingRecords.add(record);});
}
function changeScale(next){
  if(!state.pdf)return;next=Math.max(.25,Math.min(3,next));if(Math.abs(state.scale-next)<.002){scheduleViewport();return;}
  stopImageMode();
  const anchor=scrollAnchor();pinnedPages.clear();const selection=window.getSelection();if(selection?.anchorNode&&el('reader-pages').contains(selection.anchorNode))selection.removeAllRanges();
  state.scale=next;state.rendering=true;for(const record of liveRecords)evictPage(record);for(const record of waitingRecords)settlePage(record,false);reflow(anchor);updateControls();scheduleViewport();
}
function queueFit(){clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(state.pdf&&!el('reader-view').hidden&&document.documentElement.dataset.windowMode!=='floating'){if(state.fit)changeScale(fitScale());else scheduleViewport();}},120);}
function rangeText(range){
  // Range.toString omits TextLayer's BRs and can join words across PDF lines.
  const fragment=range.cloneContents(),walker=document.createTreeWalker(fragment,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT);let node,text='';
  while((node=walker.nextNode())){if(node.nodeType===Node.TEXT_NODE)text+=node.textContent;else if(node.nodeName==='BR')text+='\n';}
  return text.replace(/\u00ad/g,'').replace(/\r\n?/g,'\n').trim();
}
function selectedText(){
  const selection=window.getSelection(),stack=el('reader-pages');
  if(!selection||selection.isCollapsed||!stack.contains(selection.anchorNode)||!stack.contains(selection.focusNode))return '';
  const range=selection.getRangeAt(0),chunks=[],pages=[];
  for(const layer of stack.querySelectorAll('.textLayer')){
    if(!range.intersectsNode(layer))continue;const part=range.cloneRange(),bounds=document.createRange();bounds.selectNodeContents(layer);
    if(part.compareBoundaryPoints(Range.START_TO_START,bounds)<0)part.setStart(layer,0);
    if(part.compareBoundaryPoints(Range.END_TO_END,bounds)>0)part.setEnd(layer,layer.childNodes.length);
    const text=rangeText(part);if(text){chunks.push(text);pages.push(Number(layer.parentElement.dataset.pageNumber));}
  }
  pinnedPages=new Set(pages);return chunks.join('\n\n');
}
function captureSelection(){
  if(imageMode||!state.pdf||el('reader-view').hidden)return false;
  const text=selectedText();if(!text||text===state.selection)return false;
  selectedImage = null;
  cancelRequest();state.selection=text;state.translation='';el('reader-selection').textContent=text;el('reader-selection').classList.remove('reader-placeholder');el('reader-selection-count').textContent=text.length+' 字符';el('reader-translation').textContent='';
  if(text.length>limit)setStatus('这次选中了 '+text.length+' 个字符，请缩小到 '+limit+' 个字符以内。','error');else setStatus('已选中文字。','selected');
  return true;
}
function scheduleTranslation(){
  captureSelection();clearTimeout(selectionTimer);
  if(!selectedImage&&!imageMode&&el('reader-auto').checked&&state.selection&&state.selection.length<=limit&&state.status==='selected')selectionTimer=setTimeout(translateSelection,120);
}
async function translateSelection(){
  clearTimeout(selectionTimer);if(!state.document||!state.selection||state.selection.length>limit||!host.isReady())return;
  if (selectedImage && host.engineId() !== 'indextranslate') return;
  cancelRequest();const requestId=++requestSequence,documentId=state.document.id,text=state.selection;activeRequest=requestId;state.translation='';el('reader-translation').textContent='';setStatus('正在翻译…','loading');
  const opts = host.options();
  const result = selectedImage
    ? await host.invoke('reader_translate_image',{document_id:documentId,request_id:requestId,image_data:selectedImage,engine:'indextranslate',lang_in:opts.lang_in,lang_out:opts.lang_out})
    : await host.invoke('reader_translate',{...opts,document_id:documentId,request_id:requestId,text});
  if(requestId!==activeRequest||documentId!==state.document?.id||text!==state.selection)return;
  activeRequest=0;state.metrics={...Object.fromEntries(['worker_setup_ms','translation_ms','total_ms'].filter(key=>typeof result?.[key]==='number'&&Number.isFinite(result[key])).map(key=>[key,result[key]])),cache_hit:result?.cache_hit===true,worker_reused:result?.worker_reused===true};
  if(result?.ok&&result.request_id===requestId){state.translation=result.translation||'';el('reader-translation').textContent=state.translation;setStatus(state.translation?'翻译完成':'服务返回了空译文，请重试。',state.translation?'done':'error');}
  else if(result?.cancelled)setStatus('已取消，可重新翻译。','cancelled');
  else setStatus(result?.error||'翻译未完成，请检查引擎设置后重试。','error');
}
function prepareReader(delay=500){clearTimeout(warmTimer);warmTimer=setTimeout(()=>{if(state.document&&host.isReady())void host.invoke('reader_prepare',host.options());},delay);}

function updateImageTool() {
  const tool = el('reader-image-tool');
  tool.hidden = host.engineId() !== 'indextranslate';
  el('reader-privacy-note').textContent = tool.hidden ? '仅将选中文字发送至所选引擎。' : '选中文字或框选图片后，相应内容会发送至 B站。';
  tool.disabled = !state.pdf || !host.isReady();
  tool.setAttribute('aria-pressed',String(imageMode));
  tool.textContent = imageMode ? '取消框选' : '框选图片翻译';
}

function stopImageMode() {
  const drag = imageDrag;
  imageDrag = null;
  if (drag) {
    drag.box.remove();
    if (drag.viewport.hasPointerCapture(drag.pointerId)) drag.viewport.releasePointerCapture(drag.pointerId);
  }
  if (imageMode) {
    el('reader-crop-hint').hidden = true;
    imageMode = false;
    el('reader-viewport').classList.remove('selecting-image');
    updateImageTool();
  }
}

function imagePoint(event,rect) {
  return {
    x:Math.min(rect.width,Math.max(0,event.clientX-rect.left)),
    y:Math.min(rect.height,Math.max(0,event.clientY-rect.top))
  };
}

function updateDrag(event) {
  const drag = imageDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  const rect = drag.canvas.getBoundingClientRect();
  const point = imagePoint(event,rect);
  const x = Math.min(point.x,drag.start.x);
  const y = Math.min(point.y,drag.start.y);
  const width = Math.abs(point.x-drag.start.x);
  const height = Math.abs(point.y-drag.start.y);
  Object.assign(drag.box.style,{left:x+'px',top:y+'px',width:width+'px',height:height+'px'});
  return {x,y,width,height,rect};
}

function startImageDrag(event) {
  if (!imageMode || imageDrag || event.button !== 0 || !event.isPrimary) return;
  const page = event.target.closest('.reader-page[data-rendered="true"]');
  const canvas = page?.querySelector('canvas');
  if (!canvas) return;
  const viewport = el('reader-viewport');
  event.preventDefault();
  const box = document.createElement('div');
  box.className = 'reader-crop-box';
  page.append(box);
  imageDrag = {pointerId:event.pointerId,viewport,canvas,box,page:page.dataset.pageNumber,
    documentId:state.document.id,start:imagePoint(event,canvas.getBoundingClientRect())};
  viewport.setPointerCapture(event.pointerId);
  updateDrag(event);
}

function finishImageDrag(event) {
  const drag = imageDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  event.preventDefault();
  const bounds = updateDrag(event);
  const valid = drag.documentId===state.document?.id && host.engineId()==='indextranslate'
    && drag.canvas.isConnected && bounds.width>=8 && bounds.height>=8;
  stopImageMode();
  if (!valid) {
    pageStatus('请重新框选一块较大的图片区域。');
    return;
  }
  try {
    const sx = drag.canvas.width / bounds.rect.width;
    const sy = drag.canvas.height / bounds.rect.height;
    const width = bounds.width * sx, height = bounds.height * sy;
    const scale = Math.min(1,1400 / Math.max(width,height));
    const crop = document.createElement('canvas');
    crop.width = Math.max(1,Math.round(width*scale));
    crop.height = Math.max(1,Math.round(height*scale));
    const ctx = crop.getContext('2d',{alpha:false});
    ctx.fillStyle = '#fff';
    ctx.fillRect(0,0,crop.width,crop.height);
    ctx.drawImage(drag.canvas,bounds.x*sx,bounds.y*sy,width,height,0,0,crop.width,crop.height);
    clearSelection();
    selectedImage = crop.toDataURL('image/jpeg',.88);
    crop.width = crop.height = 0;
    state.selection = '第 '+drag.page+' 页 · 框选图片';
    el('reader-selection').textContent = state.selection;
    el('reader-selection').classList.remove('reader-placeholder');
    if (el('reader-body').classList.contains('panel-collapsed')) togglePanel();
    setStatus('已框选图片。','selected');
    pageStatus('');
    void translateSelection();
  } catch (_) {
    clearSelection();
    setStatus('无法读取这块图片，请重新框选。','error');
  }
}

el('reader-image-tool').addEventListener('click',()=>{
  if (imageMode) {
    stopImageMode();
    pageStatus('');
    return;
  }
  if (!state.pdf || host.engineId()!=='indextranslate') return;
  clearSelection();
  window.getSelection()?.removeAllRanges();
  imageMode = true;
  el('reader-viewport').classList.add('selecting-image');
  updateImageTool();
  el('reader-crop-hint').hidden = false;
});
const imageViewport = el('reader-viewport');
imageViewport.addEventListener('pointerdown',startImageDrag);
imageViewport.addEventListener('pointermove',updateDrag);
imageViewport.addEventListener('pointerup',finishImageDrag);
imageViewport.addEventListener('pointercancel',()=>{stopImageMode();pageStatus('');});
imageViewport.addEventListener('lostpointercapture',()=>{if(imageDrag){stopImageMode();pageStatus('');}});
document.addEventListener('keydown',event=>{
  if (event.key==='Escape' && imageMode) {
    stopImageMode();
    pageStatus('');
  }
});
new MutationObserver(()=>{
  if(document.documentElement.dataset.windowMode==='floating' && imageMode) {
    stopImageMode();
    pageStatus('');
  }
}).observe(document.documentElement,{attributes:true,attributeFilter:['data-window-mode']});
function settingsChanged(){stopImageMode();if(selectedImage&&host.engineId()!=='indextranslate')clearSelection();syncSettings();prepareReader();if(state.selection){cancelRequest();state.translation='';el('reader-translation').textContent='';setStatus('设置已更改，可重新翻译。','selected');}}
function togglePanel(){const collapsed=el('reader-body').classList.toggle('panel-collapsed');el('reader-panel-toggle').textContent=collapsed?'展开译文':'收起译文';el('reader-panel-toggle').setAttribute('aria-expanded',String(!collapsed));queueFit();}
function resizePanel(width){width=Math.round(Math.max(250,Math.min(460,el('reader-body').clientWidth-290,width)));el('reader-body').style.setProperty('--reader-panel-width',width+'px');el('reader-divider').setAttribute('aria-valuenow',String(width));queueFit();}

el('nav-reader').onclick=()=>showView('reader');
el('nav-workspace').onclick=event=>{event.preventDefault();showView('workspace');document.querySelector('main').scrollTo({top:0});};
el('nav-history').onclick=event=>{event.preventDefault();showView('history');};
el('reader-open').onclick=pickDocument;el('reader-empty-open').onclick=pickDocument;el('reader-close').onclick=closeDocument;
el('reader-prev').onclick=()=>goToPage(state.page-1);el('reader-next').onclick=()=>goToPage(state.page+1);
el('reader-page-number').onchange=()=>goToPage(el('reader-page-number').value);
el('reader-page-number').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();void goToPage(el('reader-page-number').value);}};
el('reader-fit').onclick=()=>{state.fit=true;changeScale(fitScale());};
for(const [id,delta]of [['reader-zoom-in',.15],['reader-zoom-out',-.15]])el(id).onclick=()=>{state.fit=false;changeScale(state.scale+delta);};
el('reader-panel-toggle').onclick=togglePanel;
el('reader-theme').onclick=()=>{el('theme-toggle').click();syncSettings();};
el('reader-translate').onclick=translateSelection;
el('reader-cancel').onclick=()=>{cancelRequest();setStatus('已取消，可重新翻译。','cancelled');};
el('reader-auto').onchange=()=>{if(el('reader-auto').checked)scheduleTranslation();else clearTimeout(selectionTimer);};
el('reader-copy').onclick=async()=>{try{await navigator.clipboard.writeText(state.translation);host.toast('译文已复制。');}catch(_){host.toast('无法自动复制，请选中译文后按 Ctrl+C。');}};
for(const suffix of ['in','out'])el('reader-lang-'+suffix).onchange=()=>{const original=el('lang-'+suffix);original.value=el('reader-lang-'+suffix).value;original.dispatchEvent(new Event('change'));settingsChanged();};
for(const id of ['engine','lang-in','lang-out'])el(id).addEventListener('change',settingsChanged);
el('engine-fields').addEventListener('input',settingsChanged);
document.addEventListener('selectionchange',()=>{const selection=window.getSelection();if(!selection||selection.isCollapsed||!el('reader-pages').contains(selection.anchorNode)){pinnedPages.clear();scheduleViewport();}else captureSelection();});
el('reader-viewport').addEventListener('scroll',scheduleViewport,{passive:true});
el('reader-viewport').addEventListener('mousedown',()=>{selectingOnPage=true;});
document.addEventListener('mouseup',event=>{if((selectingOnPage||el('reader-viewport').contains(event.target))&&selectedText()&&!el('settings-dialog').open)scheduleTranslation();selectingOnPage=false;});
el('reader-viewport').addEventListener('keyup',scheduleTranslation);
el('reader-divider').addEventListener('pointerdown',event=>{if(event.button!==0)return;event.preventDefault();el('reader-divider').setPointerCapture(event.pointerId);});
el('reader-divider').addEventListener('pointermove',event=>{if(el('reader-divider').hasPointerCapture(event.pointerId))resizePanel(el('reader-body').getBoundingClientRect().right-event.clientX);});
el('reader-divider').addEventListener('pointerup',event=>{if(el('reader-divider').hasPointerCapture(event.pointerId))el('reader-divider').releasePointerCapture(event.pointerId);});
el('reader-divider').addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();resizePanel(el('reader-translation-panel').offsetWidth+(event.key==='ArrowLeft'?20:-20));}});
new ResizeObserver(queueFit).observe(el('reader-viewport'));
new MutationObserver(syncSettings).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
window.orcaReader={openDocument,closeDocument,goToPage,show:()=>showView('reader'),snapshot:()=>({kind:selectedImage?'image':'text',document_id:state.document?.id||null,page:state.page,pages:state.pdf?.numPages||0,selection:state.selection,translation:state.translation,status:state.status,translating:state.status==='loading',rendering:state.rendering,rendered_pages:[...liveRecords].filter(record=>record.status==='ready').map(record=>record.number).sort((a,b)=>a-b),textLayers:el('reader-pages').querySelectorAll('.textLayer').length,selection_pages:[...pinnedPages],scroll_top:el('reader-viewport').scrollTop,metrics:state.metrics})};
syncSettings();updateControls();setInterval(()=>{if(!el('reader-view').hidden)syncSettings();},1000);
window.__orcaReaderReady=true;
