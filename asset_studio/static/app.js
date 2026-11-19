'use strict';
const $ = (selector) => document.querySelector(selector);
const state = {status:null,jobs:[],assets:[],reference:null,filter:'All',logId:null,assetId:null,view:'create',uploading:false,polling:false};
const esc = (value) => String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const title = value => ({balanced:'Balanced',legacy:'Quick study',detail:'Detail',master:'Master'})[value]||value;
const duration = seconds => seconds<60?`${Math.floor(seconds)}s`:`${Math.floor(seconds/60)}m ${Math.floor(seconds%60)}s`;
const elapsed = job => duration(Math.max(0,(job.finished_at||Date.now()/1000)-(job.started_at||job.created_at)));
let toastTimer;

async function api(path,method='GET',data){
  const response=await fetch('/api'+path,{method,headers:data===undefined?{}:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});
  if(!response.ok){let message='Something went wrong. Please try again.';try{const error=await response.json();message=typeof error.detail==='string'?error.detail:JSON.stringify(error.detail);}catch{}throw new Error(message);}
  return response.json();
}
function toast(message,error=false){clearTimeout(toastTimer);$('#toast').textContent=message;$('#toast').className='toast'+(error?' error':'');toastTimer=setTimeout(()=>$('#toast').classList.add('hidden'),5500);}
function navigate(){
  const requested=location.hash.slice(1)||'create';state.view=['create','queue','library','connect'].includes(requested)?requested:'create';
  document.querySelectorAll('.view').forEach(v=>v.classList.toggle('hidden',v.id!=='view-'+state.view));
  document.querySelectorAll('nav [data-view]').forEach(a=>a.classList.toggle('active',a.dataset.view===state.view));
  $('#breadcrumb').textContent={create:'Create asset',queue:'Generation queue',library:'Asset library',connect:'Agent connection'}[state.view];
  if(state.view==='library')renderLibrary();
}
function pill(status){return `<span class="status-pill ${esc(status)}">${esc(({completed:'Ready for review',queued:'Queued',running:'Generating',cancelling:'Stopping',interrupted:'Interrupted',cancelled:'Cancelled',failed:'Needs attention'})[status]||status)}</span>`;}
function activeMarkup(job,compact=false){
  if(!job)return `<div class="idle"><div class="idle-icon">✧</div><h3>Ready for the next idea.</h3><p>Add a reference to begin. Your queued assets will appear here.</p></div>`;
  return `<div class="${compact?'':'panel active-box'}"><div class="active-label">${pill(job.status)}<span class="muted">${esc(elapsed(job))} elapsed</span></div><h3 class="active-name">${esc(job.name)}</h3><div class="active-stage">${esc(job.stage||'Starting the local generator…')}</div><div class="indeterminate" aria-label="Generation in progress"></div><div class="active-meta">${esc(title(job.profile))} · ${esc(job.category)} · seed ${esc(job.seed)}</div><div class="active-footer"><span class="muted">Sources and logs are saved</span><div class="job-actions"><button class="button secondary small" data-action="log" data-id="${job.id}">Live log</button><button class="button danger small" data-action="cancel" data-id="${job.id}" ${job.status==='cancelling'?'disabled':''}>Cancel</button></div></div></div>`;
}
function renderStatus(){
  const s=state.status;if(!s)return;
  const active=s.active_job;
  $('#queue-count').textContent=s.queued+(active?1:0);$('#library-count').textContent=state.assets.length;
  $('#top-status').textContent=!s.generator_ready?'Generator setup needed':active?'Local generation in progress':s.paused?'Queue paused':s.external_generator_busy?'Local generator busy':'Local generator ready';
  $('#mcp-state').textContent='Ready';$('#activity').innerHTML=activeMarkup(active,true);
  if(!active&&s.queued){$('#activity').innerHTML=`<div class="idle"><div class="idle-icon">◷</div><h3>${s.paused?'Taking a breather.':s.external_generator_busy?'Generator already at work.':'Preparing the next asset.'}</h3><p>${s.queued} asset${s.queued===1?'':'s'} waiting.${s.paused?' Resume the queue when you’re ready.':s.external_generator_busy?' The queue will start when the current local generation finishes.':''}</p></div>`;}
  $('#mcp-url').value=s.mcp_url;
  const port=new URL(s.url).port;
  $('#mcp-config').textContent=JSON.stringify({mcpServers:{'trellislocalstudio-assets':{command:s.project_root+'/run-asset-studio-mcp.command',args:port==='8766'?[]:['--port',port],env:{TRELLIS_STUDIO_DATA:s.data_dir}}}},null,2);
}
function card(asset){
  return `<button class="asset-card" data-action="inspect" data-id="${asset.id}" aria-label="Inspect ${esc(asset.name)}"><div class="asset-image"><img loading="lazy" src="${asset.poster_url}" alt="${esc(asset.job_id?'Reference for '+asset.name:asset.name+' model preview')}"><span class="asset-category">${esc(asset.category)}</span></div><div class="asset-copy"><strong>${esc(asset.name)}</strong><small>${asset.job_id?'Generated · click to inspect in 3D':'Cleaned source · in 3D projects'}</small></div></button>`;
}
function renderLibrary(){
  const search=$('#asset-search').value.trim().toLowerCase();
  const items=state.assets.filter(a=>(state.filter==='All'||a.category===state.filter)&&(!search||(a.name+' '+a.notes).toLowerCase().includes(search)));
  $('#asset-grid').innerHTML=items.length?items.map(card).join(''):'<div class="empty-library">No assets here yet. Create one from a reference image.</div>';
  $('#recent-assets').innerHTML=state.assets.slice(0,2).map(card).join('')||'<p class="muted">Your first completed assets will appear here.</p>';
}
function jobRow(job,index,queued){
  let actions=queued?`<button class="icon-button" data-action="move-up" data-id="${job.id}" aria-label="Move ${esc(job.name)} earlier" ${index===0?'disabled':''}>⌃</button><button class="icon-button" data-action="move-down" data-id="${job.id}" aria-label="Move ${esc(job.name)} later" ${index===state.jobs.filter(j=>j.status==='queued').length-1?'disabled':''}>⌄</button><button class="button danger small" data-action="cancel" data-id="${job.id}">Cancel</button>`:
    `<button class="button secondary small" data-action="log" data-id="${job.id}">Log</button>`;
  if(['failed','interrupted','cancelled'].includes(job.status))actions+=`<button class="button secondary small" data-action="retry" data-id="${job.id}">Retry</button>`;
  if(job.status==='completed')actions+=`<button class="button secondary small" data-action="inspect" data-id="${job.id}">Inspect</button>`;
  return `<div class="job-row"><img class="job-thumb" src="${job.reference_url}" alt=""><div class="job-copy"><strong>${esc(job.name)}</strong><div class="job-subtitle"><span>${esc(title(job.profile))}</span><span>${esc(job.category)}</span><span>${queued?'Seed '+job.seed:job.started_at?elapsed(job):'Not started'}</span></div>${!queued&&job.error?`<div class="error-text">${esc(job.error)}</div>`:''}</div>${!queued?pill(job.status):''}<div class="job-actions">${actions}</div></div>`;
}
function renderQueue(){
  if(!state.status)return;
  const s=state.status,queued=state.jobs.filter(j=>j.status==='queued'),past=state.jobs.filter(j=>['completed','failed','interrupted','cancelled'].includes(j.status)).sort((a,b)=>b.created_at-a.created_at);
  $('#pause-button').textContent=s.paused?'Resume queue':'Pause queue';
  $('#queue-banner').innerHTML=s.paused?'<div class="notice">Queue paused. The current generation can finish; waiting assets stay in order.</div>':!s.generator_ready?'<div class="notice error">The local generator needs setup. References and queued jobs are saved.</div>':'';
  $('#queue-active').innerHTML=s.active_job?activeMarkup(s.active_job):'';
  $('#pending-count').textContent=queued.length+' waiting';
  $('#pending-jobs').innerHTML=queued.length?queued.map((j,i)=>jobRow(j,i,true)).join(''):'<div class="empty">Nothing waiting. <a href="#create" class="text-link">Create a new asset</a></div>';
  $('#past-jobs').innerHTML=past.length?past.map((j,i)=>jobRow(j,i,false)).join(''):'<div class="empty">Your generation history will appear here.</div>';
}
async function refresh(){
  if(state.polling)return;state.polling=true;
  try{
    const [status,jobs,assets]=await Promise.all([api('/status'),api('/jobs'),api('/assets')]);
    state.status=status;state.jobs=jobs.jobs;state.assets=assets.assets;
    $('#offline').classList.add('hidden');renderStatus();renderQueue();
    const libraryKey=JSON.stringify(state.assets);if(state.libraryKey!==libraryKey){state.libraryKey=libraryKey;renderLibrary();}
    if(state.logId)await refreshLog();
  }catch(error){$('#offline').classList.remove('hidden');$('#top-status').textContent='Disconnected';$('#mcp-state').textContent='Offline';}
  finally{state.polling=false;}
}
function clearReference(){
  state.reference=null;$('#reference-file').value='';$('#reference-preview').classList.add('hidden');$('#reference-name').classList.add('hidden');$('#drop-placeholder').classList.remove('hidden');$('#reference-preview').removeAttribute('src');$('#enqueue-button').disabled=true;
}
async function upload(file){
  if(!file||state.uploading)return;
  if(file.size>20*1024**2){toast('Choose an image smaller than 20 MB.',true);return;}
  state.uploading=true;$('#enqueue-button').disabled=true;$('#drop-placeholder strong').textContent='Preparing reference…';
  try{
    const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
    const ref=await api('/references','POST',{filename:file.name,data});setReference(ref);
    if(!$('#asset-name').value)$('#asset-name').value=file.name.replace(/\.[^.]+$/,'').replace(/[-_]/g,' ').slice(0,100);
  }catch(error){toast(error.message||'Could not upload this image.',true);$('#enqueue-button').disabled=!state.reference;}
  finally{state.uploading=false;$('#drop-placeholder strong').textContent='Drop your reference here';}
}
function setReference(ref){
  state.reference=ref;$('#reference-preview').src=ref.url;$('#reference-preview').classList.remove('hidden');$('#reference-name').textContent=`${ref.filename} · ${ref.width} × ${ref.height} · click to replace`;$('#reference-name').classList.remove('hidden');$('#drop-placeholder').classList.add('hidden');$('#enqueue-button').disabled=false;
}
async function refreshLog(){
  const data=await api('/jobs/'+state.logId+'/log');if(!state.logId)return;
  const job=state.jobs.find(j=>j.id===state.logId);$('#log-job-status').textContent=(job?.stage||data.status)+(job?.error?' · '+job.error:'');
  const pre=$('#log-text'),atBottom=pre.scrollHeight-pre.scrollTop-pre.clientHeight<60;pre.textContent=data.text.trim()||'Waiting for the generator to write its first stage…';if(atBottom)pre.scrollTop=pre.scrollHeight;
}
async function openAsset(id){
  const asset=state.assets.find(a=>a.id===id);if(!asset)throw new Error('Asset is not in the library yet.');
  state.assetId=id;$('#asset-dialog-name').textContent=asset.name;$('#asset-dialog-category').textContent=asset.category;$('#asset-dialog-notes').textContent=asset.notes||'Generated source asset. Inspect from every side before cleanup and game integration.';
  $('#asset-dialog-meta').textContent=(asset.triangles?asset.triangles.toLocaleString()+' triangles · ':'')+(asset.job_id?'Generated source · review pending':'Cleaned and integrated source')+' · GLB';
  $('#download-button').href=asset.model_url;$('#download-button').download=asset.name+'.glb';
  const viewer=document.createElement('model-viewer');viewer.setAttribute('camera-controls','');viewer.setAttribute('shadow-intensity','.7');viewer.setAttribute('exposure','1');viewer.setAttribute('environment-image','neutral');viewer.setAttribute('interaction-prompt','none');viewer.setAttribute('alt','Orbitable 3D model of '+asset.name);
  $('#viewer-status').textContent='Loading model from this Mac…';
  viewer.addEventListener('load',()=>{$('#viewer-status').textContent='Drag to orbit · scroll to zoom · right-drag to pan';});
  viewer.addEventListener('error',()=>{$('#viewer-status').textContent='Preview could not load. The GLB is still available to download.';});
  $('#viewer-area').replaceChildren(viewer);$('#asset-dialog').showModal();viewer.setAttribute('src',asset.model_url);
}
async function action(name,id){
  if(name==='cancel'){const job=state.jobs.find(j=>j.id===id);if(job&&['running','cancelling'].includes(job.status)&&!confirm('Stop this generation? Partial files will be kept, and you can retry later.'))return;await api('/jobs/'+id+'/cancel','POST',{});toast('Cancellation requested.');}
  if(name==='retry'){await api('/jobs/'+id+'/retry','POST',{});toast('New attempt added to the queue.');}
  if(name==='move-up'||name==='move-down'){const ids=state.jobs.filter(j=>j.status==='queued').map(j=>j.id),index=ids.indexOf(id),other=index+(name==='move-up'?-1:1);if(other<0||other>=ids.length)return;[ids[index],ids[other]]=[ids[other],ids[index]];await api('/queue/order','POST',{ordered_ids:ids});}
  if(name==='log'){state.logId=id;$('#log-title').textContent=state.jobs.find(j=>j.id===id)?.name||'Generation';$('#log-dialog').showModal();await refreshLog();return;}
  if(name==='inspect'){await openAsset(id);return;}
  if(name==='close-asset'){$('#asset-dialog').close();return;}
  if(name==='close-log'){$('#log-dialog').close();return;}
  if(name==='copy-url'||name==='copy-config'){await navigator.clipboard.writeText(name==='copy-url'?$('#mcp-url').value:$('#mcp-config').textContent);toast('Copied to clipboard.');return;}
  await refresh();
}

document.addEventListener('click',event=>{
  const target=event.target.closest('[data-action]');if(target&&!target.disabled)action(target.dataset.action,target.dataset.id).catch(error=>toast(error.message,true));
  const filter=event.target.closest('[data-filter]');if(filter){state.filter=filter.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===filter));renderLibrary();}
});
$('#create-form').addEventListener('submit',async event=>{
  event.preventDefault();if(!state.reference)return;
  $('#enqueue-button').disabled=true;$('#enqueue-button').textContent='Adding to queue…';$('#form-error').classList.add('hidden');
  try{
    const job=await api('/jobs','POST',{name:$('#asset-name').value.trim(),category:$('#category').value,reference_id:state.reference.id,profile:$('#quality').value,seed:Number($('#seed').value),memory_gib:Number($('#memory').value),notes:$('#notes').value});
    toast(job.name+' added to the queue.');$('#asset-name').value='';$('#notes').value='';clearReference();await refresh();
  }catch(error){$('#form-error').textContent=error.message;$('#form-error').classList.remove('hidden');$('#enqueue-button').disabled=false;}
  finally{$('#enqueue-button').textContent='Add to generation queue';}
});
$('#pause-button').addEventListener('click',async()=>{try{await api('/queue','PATCH',{paused:!state.status.paused});await refresh();}catch(error){toast(error.message,true);}});
$('#quality').addEventListener('change',()=>{const value=$('#quality').value,note=$('#quality-help');note.classList.toggle('warning',['detail','master'].includes(value));note.querySelector('p').textContent=value==='balanced'?'Our verified recipe. Full precision, 4K textures and preserved source geometry.':value==='legacy'?'A lighter, quantized study with 1K textures. Useful for checking shape.':'Experimental on this 16 GB Mac. May stop at the memory limit; quality will never be silently reduced.';});
$('#asset-search').addEventListener('input',renderLibrary);
$('#dropzone').addEventListener('click',()=>$('#reference-file').click());
$('#dropzone').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('#reference-file').click();}});
$('#reference-file').addEventListener('change',e=>upload(e.target.files[0]));
for(const name of ['dragover','dragenter'])$('#dropzone').addEventListener(name,e=>{e.preventDefault();$('#dropzone').classList.add('dragging');});
for(const name of ['dragleave','drop'])$('#dropzone').addEventListener(name,e=>{e.preventDefault();$('#dropzone').classList.remove('dragging');});
$('#dropzone').addEventListener('drop',e=>upload(e.dataTransfer.files[0]));
$('#asset-dialog').addEventListener('close',()=>{$('#viewer-area').replaceChildren();state.assetId=null;});
$('#log-dialog').addEventListener('close',()=>{state.logId=null;});
$('#reuse-button').addEventListener('click',async()=>{try{const asset=state.assets.find(a=>a.id===state.assetId),ref=await api('/assets/'+state.assetId+'/reuse','POST',{});setReference(ref);$('#asset-name').value=asset.name+' variation';$('#category').value=asset.category;$('#notes').value=asset.notes||'';$('#asset-dialog').close();location.hash='create';toast('Reference ready. Choose settings and add your variation.');}catch(error){toast(error.message,true);}});
for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}});
window.addEventListener('hashchange',navigate);navigate();refresh();setInterval(refresh,2500);
