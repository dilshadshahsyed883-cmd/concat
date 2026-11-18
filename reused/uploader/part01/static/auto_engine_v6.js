/* LUM_FULL_AUTO_ENGINE_V6_1_UI */
(() => {
  'use strict';
  const AE_KEY=window.location.pathname.startsWith('/short-uploader') ? 'lum.auto.v6.panel.shortstudio' : window.location.pathname.startsWith('/song-uploader') ? 'lum.auto.v6.panel.song' : window.location.pathname.startsWith('/local-upload-manager/modes/')
    ? 'lum.auto.v6.panel.'+window.location.pathname.split('/')[3]
    : 'lum.auto.v6.panel';
  const panels=['automation','profiles','queue','records'];
  const legacyProfilePanels=new Set(['setup','sources','metadata','schedule','thumbnails']);
  const normalizePanel=v=>v==='auto'?'automation':(legacyProfilePanels.has(v)?'profiles':(panels.includes(v)?v:'automation'));
  const qSelected=new Set();
  const tSelected=new Set();

  const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const basePath=()=>window.location.pathname.replace(/\/app$/,'');
  const notify=()=>{ try{window.parent.postMessage({type:'lum-resize'},'*')}catch(_){} };

  function setPanel(name,persist=true){
    let wanted=normalizePanel(name);
    if(!document.querySelector(`[data-ae-panel="${wanted}"]`)) wanted='automation';
    document.querySelectorAll('[data-ae-panel]').forEach(el=>el.hidden=el.dataset.aePanel!==wanted);
    document.querySelectorAll('[data-ae-panel-target]').forEach(btn=>{
      const active=btn.dataset.aePanelTarget===wanted;
      btn.classList.toggle('active',active); btn.setAttribute('aria-selected',active?'true':'false');
    });
    const savebar=document.querySelector('[data-ae-profile-savebar]');
    if(savebar) savebar.hidden=!['setup','sources','metadata','schedule'].includes(wanted);
    if(persist){try{localStorage.setItem(AE_KEY,wanted)}catch(_){}}
    notify();
  }

  function restorePanel(){
    let value='automation'; try{value=localStorage.getItem(AE_KEY)||'automation'}catch(_){}
    setPanel(normalizePanel(value),false);
  }

  function alarmValues(){
    const hidden=document.getElementById('lum-ae-schedule-times');
    return (hidden?.value||'').split(',').map(x=>x.trim()).filter(Boolean).filter((v,i,a)=>a.indexOf(v)===i).sort();
  }
  function renderAlarms(values=alarmValues()){
    const list=document.getElementById('lum-ae-alarm-list'), hidden=document.getElementById('lum-ae-schedule-times');
    if(!list||!hidden)return;
    hidden.value=values.join(',');
    list.innerHTML=values.map(v=>`<span class="lum-ae-alarm">${esc(v)}<button type="button" data-ae-remove-time="${esc(v)}" aria-label="Remove ${esc(v)}">×</button></span>`).join('') || '<span class="lum-note">No alarm yet.</span>';
  }

  function softGoto(url){
    const a=document.createElement('a'); a.href=url; a.setAttribute('data-lum-soft-link',''); a.style.display='none'; document.body.appendChild(a); a.click(); setTimeout(()=>a.remove(),0);
  }

  function updateQ(){
    document.querySelectorAll('.lum-ae-queue-select').forEach(cb=>cb.checked=qSelected.has(Number(cb.value)));
    document.querySelectorAll('[data-ae-queue-count]').forEach(el=>el.textContent=`${qSelected.size} selected`);
  }
  function updateT(){
    document.querySelectorAll('.lum-ae-thumb-select').forEach(cb=>cb.checked=tSelected.has(Number(cb.value)));
    document.querySelectorAll('[data-ae-thumb-count]').forEach(el=>el.textContent=`${tSelected.size} selected`);
  }

  function addIds(form,set,name,required){
    form.querySelectorAll(`input[data-ae-added="${name}"]`).forEach(x=>x.remove());
    if(required && !set.size){alert('Select one or more items first.'); return false;}
    [...set].sort((a,b)=>a-b).forEach(id=>{
      const x=document.createElement('input');x.type='hidden';x.name=name;x.value=String(id);x.dataset.aeAdded=name;form.appendChild(x);
    });
    return true;
  }

  /* LUM_PROFILE_COUNTS_LIVE_V1 */
  function updateProfileCounts(profileId,data){
    const pid=String(profileId??'').trim(); if(!pid)return;
    const plan=(data&&data.plan)||data||{}; const scan=(data&&data.video_scan)||{};
    const videos=plan.available_videos!==undefined?Number(plan.available_videos||0):(scan.videos!==undefined?Number(scan.videos||0):null);
    const thumbs=plan.active_thumbnails!==undefined?Number(plan.active_thumbnails||0):null;
    const vc=document.querySelector(`[data-ae-profile-videos="${pid}"]`);
    const tc=document.querySelector(`[data-ae-profile-thumbs="${pid}"]`);
    if(videos!==null&&vc)vc.textContent=String(videos);
    if(thumbs!==null&&tc)tc.textContent=String(thumbs);
    const chip=document.querySelector(`[data-ae-profile-counts="${pid}"]`);
    if(chip){
      const v=videos!==null?videos:Number(vc?.textContent||0);
      const t=thumbs!==null?thumbs:Number(tc?.textContent||0);
      chip.textContent=`${v} videos · ${t} thumbs`;
    }
  }

  function renderResult(data,targetId,profileId){
    const target=document.getElementById(targetId); if(!target)return;
    const errors=data.errors||data.blocked||data.issues||[]; const warnings=data.warnings||[];
    const plan=data.plan||data;
    const scan=data.video_scan||{}; const thumbs=data.thumbnail_sync||{};
    updateProfileCounts(profileId,data);
    target.innerHTML=`<div class="lum-ae-result-grid">
      ${scan.videos!==undefined?`<div><span>Drive videos</span><strong>${Number(scan.videos||0)}</strong></div>`:''}
      ${plan.available_videos!==undefined?`<div><span>Available videos</span><strong>${Number(plan.available_videos||0)}</strong></div>`:''}
      ${plan.eligible_channels!==undefined?`<div><span>Eligible channels</span><strong>${Number(plan.eligible_channels||0)}</strong></div>`:''}
      ${plan.active_thumbnails!==undefined?`<div><span>Active thumbnails</span><strong>${Number(plan.active_thumbnails||0)}</strong></div>`:''}
      ${plan.target_jobs!==undefined?`<div><span>Requested jobs</span><strong>${Number(plan.target_jobs||0)}</strong></div>`:''}
      ${plan.duplicates_blocked!==undefined?`<div><span>Duplicates blocked</span><strong>${Number(plan.duplicates_blocked||0)}</strong></div>`:''}
      ${thumbs.installed!==undefined?`<div><span>Thumbs synced</span><strong>${Number(thumbs.installed||0)}</strong></div>`:''}
    </div>
    ${errors.length?`<div class="lum-alert error"><strong>Blocked:</strong> ${errors.map(esc).join(' · ')}</div>`:'<div class="lum-alert success">Ready / no blocking error.</div>'}
    ${warnings.length?`<div class="lum-alert warn"><strong>Warnings:</strong> ${warnings.map(esc).join(' · ')}</div>`:''}`;
    notify();
  }

  function renderPreview(data){
    const target=document.getElementById('lum-ae-preview-result');if(!target)return;
    const jobs=data.jobs||[]; const blocked=data.blocked||[];
    let html=`<div class="lum-ae-result-grid"><div><span>Jobs</span><strong>${jobs.length}</strong></div><div><span>Target</span><strong>${Number(data.target_jobs||0)}</strong></div><div><span>Duplicate blocks</span><strong>${Number(data.duplicates_blocked||0)}</strong></div><div><span>Eligible channels</span><strong>${Number(data.eligible_channels||0)}</strong></div></div>`;
    if(blocked.length)html+=`<div class="lum-alert warn"><strong>Planner notes:</strong> ${blocked.map(esc).join(' · ')}</div>`;
    if(jobs.length){
      html+=`<div class="lum-table-wrap"><table class="lum-table lum-ae-preview-table"><thead><tr><th>#</th><th>Channel</th><th>Drive Video</th><th>Thumbnail</th><th>Resolved Title</th><th>Privacy</th><th>Playlist</th></tr></thead><tbody>`+
      jobs.map((j,i)=>`<tr><td>${i+1}</td><td>${esc(j.channel_name)}</td><td title="${esc(j.drive_file_id)}">${esc(j.filename)}</td><td>${esc(j.thumbnail_filename||'—')}</td><td>${esc(j.title)}</td><td>${esc(j.privacy_status)}</td><td>${esc(j.playlist_id||'—')}</td></tr>`).join('')+`</tbody></table></div>`;
    }
    target.innerHTML=html;notify();
  }

  async function postJson(url,fields){
    const fd=new FormData();Object.entries(fields).forEach(([k,v])=>fd.append(k,v));
    const r=await fetch(url,{method:'POST',credentials:'same-origin',body:fd,headers:{'X-LUM-Soft-Navigation':'1'}});
    const data=await r.json().catch(()=>({errors:[`HTTP ${r.status}`]}));
    if(!r.ok && !data.errors && !data.blocked)data.errors=[`HTTP ${r.status}`];return data;
  }

  /* LUM_GENERATE_FEEDBACK_V1 */
  function generateErrors(data){
    if(!data)return ['No response from server.'];
    const out=[];
    for(const key of ['errors','blocked','issues']){
      const v=data[key]; if(Array.isArray(v))out.push(...v.filter(Boolean).map(String));
    }
    return out;
  }
  function setGenerateStatus(state,title,detail=''){
    const box=document.getElementById('lum-ae-generation-status');if(!box)return;
    box.hidden=false;box.dataset.state=state||'working';
    const h=box.querySelector('[data-ae-generation-title]');const d=box.querySelector('[data-ae-generation-detail]');
    if(h)h.textContent=title||'';if(d)d.textContent=detail||'';notify();
  }
  function updateQueueTabCount(data){
    const n=Number(data?.queue_total);if(!Number.isFinite(n))return;
    const tab=document.querySelector('[data-ae-panel-target="queue"]');if(tab){tab.dataset.aeQueueTotal=String(n);tab.textContent=n>0?`Queue (${n})`:'Queue';}
  }

  document.addEventListener('click',async e=>{
    const tab=e.target.closest('[data-ae-panel-target]');
    if(tab){e.preventDefault();e.stopPropagation();setPanel(tab.dataset.aePanelTarget,true);return;}
    const add=e.target.closest('[data-ae-add-time]');
    if(add){const input=document.getElementById('lum-ae-new-time');const v=input?.value;if(v){const vals=alarmValues();if(!vals.includes(v))vals.push(v);renderAlarms(vals.sort());}return;}
    const rem=e.target.closest('[data-ae-remove-time]');if(rem){renderAlarms(alarmValues().filter(v=>v!==rem.dataset.aeRemoveTime));return;}
    const all=e.target.closest('[data-ae-thumb-select-all]');if(all){document.querySelectorAll('.lum-ae-thumb-select').forEach(cb=>tSelected.add(Number(cb.value)));updateT();return;}
    const clear=e.target.closest('[data-ae-thumb-clear]');if(clear){tSelected.clear();updateT();return;}
    const preview=e.target.closest('[data-ae-thumb-preview]');if(preview){const box=document.getElementById('lum-ae-lightbox');if(box){box.querySelector('img').src=preview.dataset.aeThumbPreview;box.hidden=false;}return;}
    if(e.target.closest('[data-ae-close-preview]')){const box=document.getElementById('lum-ae-lightbox');if(box)box.hidden=true;return;}
    const sync=e.target.closest('[data-ae-sync-scan]');if(sync){const targetId=sync.dataset.aeResultTarget||'lum-ae-preflight-result';if(targetId==='lum-ae-preview-result')setPanel('automation',true);sync.disabled=true;try{const data=await postJson(`${basePath()}/actions/auto-v6/sync-scan`,{csrf_token:sync.dataset.csrf,profile_id:sync.dataset.profileId});renderResult(data,targetId);document.getElementById(targetId)?.scrollIntoView({behavior:'smooth',block:'center'});}finally{sync.disabled=false;}return;}
    const pf=e.target.closest('[data-ae-preflight]');if(pf){pf.disabled=true;try{const r=await fetch(`${basePath()}/actions/auto-v6/preview?profile_id=${encodeURIComponent(pf.dataset.profileId)}`,{headers:{'Accept':'application/json'}});const data=await r.json();if(!r.ok)throw new Error(data?.detail||data?.error||'Preflight failed');renderPreview(data);}catch(err){renderResult({ok:false,errors:[String(err?.message||err)]},'lum-ae-preview-result',pf.dataset.profileId);}finally{pf.disabled=false;}return;}
    const qpf=e.target.closest('[data-ae-queue-preflight]');if(qpf){setPanel('queue',true);qpf.disabled=true;try{const data=await postJson(`${basePath()}/actions/auto-v6/queue-preflight`,{csrf_token:qpf.dataset.csrf});renderResult(data,'lum-ae-queue-preflight-result');document.getElementById('lum-ae-queue-preflight-result')?.scrollIntoView({behavior:'smooth',block:'center'});}finally{qpf.disabled=false;}return;}
    const gp=e.target.closest('[data-ae-global-preflight]');if(gp){setPanel('queue',true);gp.disabled=true;try{const data=await postJson(`${basePath()}/actions/auto-v6/queue-preflight`,{csrf_token:gp.dataset.csrf});renderResult(data,'lum-ae-queue-preflight-result');document.getElementById('lum-ae-queue-preflight-result')?.scrollIntoView({behavior:'smooth',block:'center'});}finally{gp.disabled=false;}return;}
    const pv=e.target.closest('[data-ae-preview]');if(pv){if(!pv.dataset.profileId)return;setPanel('automation',true);pv.disabled=true;try{const r=await fetch(`${basePath()}/actions/auto-v6/preview?profile_id=${encodeURIComponent(pv.dataset.profileId)}`,{credentials:'same-origin'});const data=await r.json();updateProfileCounts(pv.dataset.profileId,data);renderPreview(data);document.getElementById('lum-ae-preview-result')?.scrollIntoView({behavior:'smooth',block:'center'});}finally{pv.disabled=false;}return;}
    const previewAll=e.target.closest('[data-ae-preview-all]');if(previewAll){
      e.preventDefault();
      const output=document.getElementById('lum-ae-bulk-preview');if(!output)return;
      previewAll.disabled=true;output.hidden=false;output.replaceChildren();
      const status=document.createElement('strong');status.textContent='Checking next jobs for enabled profiles…';output.append(status);
      try{
        const res=await fetch(basePath()+'/actions/auto-v6/preview-all',{credentials:'same-origin',headers:{Accept:'application/json'}});
        const data=await res.json();
        if(!res.ok||!data.ok)throw Error(data.error||'Could not preview upcoming jobs.');
        const profiles=Array.isArray(data.profiles)?data.profiles:[];
        status.textContent=Number(data.eligible_jobs||0)+' eligible next job(s) across '+profiles.length+' enabled profile(s). No queue jobs were created.';
        const list=document.createElement('ul');
        for(const item of profiles){
          const row=document.createElement('li');
          const count=Number(item.eligible_jobs||0);
          const blocked=(Array.isArray(item.blocked)?item.blocked:[]).map(v=>String(v)).filter(Boolean).slice(0,2);
          row.textContent=String(item.name||'Profile')+': '+count+' eligible'+(item.ready?' · ready':' · needs attention');
          if(blocked.length)row.append(document.createTextNode(' — '+blocked.join(' | ')));
          else if(Array.isArray(item.next)&&item.next.length){
            const examples=item.next.map(job=>[job.video,job.channel].filter(Boolean).join(' → ')).join(' · ');
            if(examples)row.append(document.createTextNode(' — '+examples));
          }
          list.append(row);
        }
        if(profiles.length)output.append(list);
      }catch(err){status.textContent=String(err?.message||err||'Preview failed');}
      finally{previewAll.disabled=false;}
      return;
    }
    const schedules=e.target.closest('[data-ae-open-schedules]');if(schedules){const dlg=document.getElementById('lum-ae-schedules-dialog');if(dlg){if(typeof dlg.showModal==='function')dlg.showModal();else dlg.setAttribute('open','');}return;}
    if(e.target.closest('[data-ae-close-schedules]')){const dlg=document.getElementById('lum-ae-schedules-dialog');if(dlg){if(typeof dlg.close==='function')dlg.close();else dlg.removeAttribute('open');}return;}
    const rowGenerate=e.target.closest('[data-ae-row-generate]');if(rowGenerate){
      e.preventDefault();e.stopPropagation();
      const profileId=String(rowGenerate.dataset.profileId||'').trim();
      const profileName=String(rowGenerate.dataset.profileName||`Profile #${profileId}`).trim();
      const csrf=String(rowGenerate.dataset.csrf||'').trim();
      if(!profileId||!csrf){setGenerateStatus('error',`${profileName}: Cannot generate`,'Missing profile or security token. Refresh the page and try again.');return;}
      if(rowGenerate.dataset.busy==='1')return;
      rowGenerate.dataset.busy='1';rowGenerate.disabled=true;
      let success=false;
      try{
        rowGenerate.textContent='Checking…';
        setGenerateStatus('working',`${profileName}: Checking sources…`,'Refreshing Drive inventory and validating videos, channels and thumbnails.');
        const pre=await postJson(`${basePath()}/actions/auto-v6/sync-scan`,{csrf_token:csrf,profile_id:profileId});
        updateProfileCounts(profileId,pre);
        const preErrors=generateErrors(pre);const plan=pre.plan||pre||{};
        if(pre.ok===false||preErrors.length||plan.ready===false){throw new Error(preErrors.join(' · ')||'Preflight is not ready.');}
        rowGenerate.textContent='Generating…';
        setGenerateStatus('working',`${profileName}: Generating queue…`,`${Number(plan.target_jobs||0)} job(s) requested. Creating durable queue records now.`);
        const data=await postJson(`${basePath()}/actions/auto-v6/generate-json`,{csrf_token:csrf,profile_id:profileId,confirmation:'GENERATE'});
        updateProfileCounts(profileId,data);
        const errors=generateErrors(data);if(data.ok===false||errors.length)throw new Error(errors.join(' · ')||'Queue generation failed.');
        const created=Number(data.created||0),dups=Number(data.duplicates_blocked||0),profileQueue=Number(data.profile_queue_total||0),queueTotal=Number(data.queue_total||0);
        rowGenerate.textContent='Generated ✓';success=true;updateQueueTabCount(data);
        setGenerateStatus('success',`${profileName}: Queue generated`,`${created} job(s) added. Profile active queue: ${profileQueue}. Total active queue: ${queueTotal}.${dups?` ${dups} duplicate(s) blocked.`:''}`);
      }catch(err){
        rowGenerate.textContent='Generate';
        setGenerateStatus('error',`${profileName}: Generation failed`,String(err?.message||err||'Unknown error'));
      }finally{
        rowGenerate.disabled=false;rowGenerate.dataset.busy='0';
        if(success)setTimeout(()=>{if(rowGenerate.dataset.busy!=='1')rowGenerate.textContent='Generate';},2600);
      }
      return;
    }
    const jumpStartAll=e.target.closest('[data-ae-jump-start-all]');if(jumpStartAll){
      const f=document.getElementById('lum-ae-start-all-form');if(!f)return;
      const v=window.prompt('Type START ALL to run all enabled profiles.');
      if(v===null)return;
      if(String(v).trim()!=='START ALL'){window.alert('Start All cancelled. Type START ALL exactly.');return;}
      const c=f.querySelector('input[name=confirmation]');if(c)c.value='START ALL';
      clearDirty();f.requestSubmit();return;
    }
  },true);

  document.addEventListener('change',e=>{
    if(e.target?.id==='lum-ae-profile-kind'||e.target?.id==='lum-ae-profile-group'){updateSongProfileKind();return;}
    if(e.target?.id==='lum-ae-profile-select'){
      const id=e.target.value;softGoto(`${basePath()}/app?tab=auto_engine${id?`&auto_profile_id=${encodeURIComponent(id)}`:''}`);return;
    }
    if(e.target?.classList.contains('lum-ae-queue-select')){const id=Number(e.target.value);e.target.checked?qSelected.add(id):qSelected.delete(id);updateQ();return;}
    if(e.target?.classList.contains('lum-ae-thumb-select')){const id=Number(e.target.value);e.target.checked?tSelected.add(id):tSelected.delete(id);updateT();return;}
    if(e.target?.id==='lum-ae-concurrency'){const form=e.target.closest('form');if(form){form.requestSubmit();}return;}
  },true);

  document.addEventListener('submit',e=>{
    const f=e.target;
    if(f.matches('[data-ae-queue-form]') && !addIds(f,qSelected,'video_ids',true)){e.preventDefault();e.stopImmediatePropagation();return;}
    if(f.matches('[data-ae-queue-optional-form]'))addIds(f,qSelected,'video_ids',false);
    if(f.matches('[data-ae-thumb-form]') && !addIds(f,tSelected,'asset_ids',true)){e.preventDefault();e.stopImmediatePropagation();return;}
  },true);

  /* LUM_QUEUE_LIVE_AUTOREFRESH_V1 */
  let queueLiveTimer=0,queueLiveBusy=false,queueLiveLast='';
  function queueLivePanel(){
    const p=document.querySelector('[data-ae-panel="queue"]');
    if(!p||p.hidden||p.getAttribute('aria-hidden')==='true')return null;
    return p;
  }
  function queueLiveBadge(panel,state='live'){
    if(!panel)return;
    let b=panel.querySelector('[data-ae-queue-live-badge]');
    if(!b){
      const refresh=[...panel.querySelectorAll('button,a')].find(x=>String(x.textContent||'').trim()==='Refresh');
      b=document.createElement('span');b.dataset.aeQueueLiveBadge='1';
      b.style.cssText='display:inline-flex;align-items:center;margin-left:8px;padding:3px 7px;border-radius:999px;font-size:9px;font-weight:700;white-space:nowrap;vertical-align:middle';
      if(refresh?.parentElement)refresh.parentElement.appendChild(b);else panel.prepend(b);
    }
    if(state==='retry'){b.textContent='● Live retrying';b.style.background='#fff4df';b.style.color='#9a6200';}
    else{b.textContent='● Live';b.style.background='#e8f8ef';b.style.color='#167047';b.title='Queue updates automatically without refreshing the page';}
  }
  async function refreshQueueLive(force=false){
    const live=queueLivePanel();if(!live||queueLiveBusy||document.hidden)return;
    const active=document.activeElement;
    if(!force&&active&&live.contains(active)&&/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(active.tagName))return;
    queueLiveBusy=true;
    try{
      const u=new URL(`${basePath()}/app`,window.location.origin);u.searchParams.set('tab','auto_engine');u.searchParams.set('_live',String(Date.now()));
      const r=await fetch(u.toString(),{credentials:'same-origin',cache:'no-store',headers:{'X-LUM-Soft-Navigation':'1'}});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const text=await r.text();const doc=new DOMParser().parseFromString(text,'text/html');
      const fresh=doc.querySelector('[data-ae-panel="queue"]');if(!fresh)throw new Error('Queue panel missing in live snapshot');
      const freshHtml=fresh.innerHTML;
      if(force||freshHtml!==queueLiveLast){
        const y=window.scrollY;
        live.innerHTML=freshHtml;queueLiveLast=freshHtml;updateQ();queueLiveBadge(live,'live');
        if(Math.abs(window.scrollY-y)>2)window.scrollTo(window.scrollX,y);
      }else queueLiveBadge(live,'live');
    }catch(_err){queueLiveBadge(live,'retry');}
    finally{queueLiveBusy=false;}
  }
  function startQueueLivePolling(){
    if(queueLiveTimer)clearInterval(queueLiveTimer);
    const p=queueLivePanel();if(p)queueLiveBadge(p,'live');
    queueLiveTimer=setInterval(()=>refreshQueueLive(false),2000);
    if(p)setTimeout(()=>refreshQueueLive(true),250);
  }

  function updateSongProfileKind(){
    const form=document.getElementById('lum-ae-profile-form');
    if(!form)return;
    const groupSelect=form.querySelector('#lum-ae-profile-group');
    if(!groupSelect)return;
    const preview=form.querySelector('#lum-ae-group-preview');
    if(preview&&groupSelect){
      const n=Number(groupSelect.selectedOptions?.[0]?.dataset?.count||0);
      preview.textContent=groupSelect.value
        ? `${n} group member(s). All eligible ACTIVE members with a valid token will be included.`
        : 'Select a group; its eligible ACTIVE members will be included.';
    }
  }
  function init(){restorePanel();renderAlarms();updateQ();updateT();updateSongProfileKind();startQueueLivePolling();}
  document.addEventListener('DOMContentLoaded',init,{once:true});
  window.addEventListener('pageshow',init);
  const observer=new MutationObserver(ms=>{for(const m of ms)for(const n of m.addedNodes){if(n instanceof Element && (n.matches?.('.lum-shell')||n.querySelector?.('[data-ae-panel]'))){queueMicrotask(init);return;}}});
  if(document.documentElement)observer.observe(document.documentElement,{childList:true,subtree:true});
  init();
})();


/* LUM_AUTO_ENGINE_PROFILE_UX_GUARD_V1 */
(()=>{
  let profileDirty=false;
  const dirtyNode=()=>document.querySelector('[data-ae-profile-dirty]');
  const markDirty=()=>{profileDirty=true;const n=dirtyNode();if(n)n.hidden=false;};
  const clearDirty=()=>{profileDirty=false;const n=dirtyNode();if(n)n.hidden=true;};
  const askLeave=()=>!profileDirty||window.confirm('This profile has unsaved changes. Leave without saving?');
  document.addEventListener('input',e=>{if(e.target.closest?.('#lum-ae-profile-form'))markDirty();},true);
  document.addEventListener('change',e=>{
    if(e.target?.id==='lum-ae-profile-select'){
      if(!askLeave()){
        e.preventDefault();e.stopImmediatePropagation();e.target.value=e.target.dataset.currentProfile||'';return;
      }
      clearDirty();return;
    }
    if(e.target.closest?.('#lum-ae-profile-form'))markDirty();
  },true);
  document.addEventListener('click',e=>{
    const nav=e.target.closest?.('[data-ae-profile-nav]');
    if(!nav)return;
    if(!askLeave()){e.preventDefault();e.stopImmediatePropagation();return;}
    clearDirty();
    if((nav.getAttribute('href')||'').includes('new_profile=1')){
      try{localStorage.setItem(AE_KEY,'profiles')}catch(_){}
    }
  },true);
  document.addEventListener('submit',e=>{if(e.target?.id==='lum-ae-profile-form')clearDirty();},true);
  window.addEventListener('beforeunload',e=>{if(!profileDirty)return;e.preventDefault();e.returnValue='';});
})();

// LUM_QUEUE_LIVE_VISIBILITY_V1
document.addEventListener('visibilitychange',()=>{if(!document.hidden)setTimeout(()=>refreshQueueLive(true),120);});

/* SONG_PROFILE_MANUAL_METADATA_V1 — affects Song Uploader only, including soft navigation. */
(() => {
  'use strict';
  function applySongProfileSources() {
    const form=document.getElementById('lum-ae-profile-form');
    if(!form) return;
    const video=form.querySelector('#song-profile-video-source');
    const meta=form.querySelector('#song-profile-metadata-source');
    if(!video||!meta) return;
    const useDrive=video.value==='drive', useSheet=meta.value==='sheet';
    const useFolder=video.value==='songfolder';
    const flip=(selector,show,requiredName)=>{
      form.querySelectorAll(selector).forEach(el=>{
        el.hidden=!show;
        el.querySelectorAll('input,textarea,select').forEach(input=>{
          if(requiredName && input.name===requiredName) input.required=show;
        });
      });
    };
    flip('[data-song-folder-field]',useFolder,'song_folder_id');
    flip('[data-song-drive-field]',useDrive,'drive_video_folder');
    const folders=form.querySelector('#song-profile-folder');
    if(folders&&!folders.dataset.loading){
      folders.dataset.loading='1';
      fetch('/song-maker/api/folders',{credentials:'same-origin',cache:'no-store'}).then(async r=>{if(!r.ok)throw Error('Folder registry unavailable');return r.json();}).then(data=>{
        if(!folders.isConnected)return;const saved=folders.value||folders.dataset.savedFolder||'';
        folders.replaceChildren(new Option('Select folder…',''));
        for(const item of data.folders||[])folders.add(new Option(item.name,item.id));
        if(saved&&![...folders.options].some(o=>o.value===saved))folders.add(new Option('Saved folder unavailable',saved));
        folders.value=saved;
      }).catch(e=>{const status=form.querySelector('[data-song-folder-status]');if(status)status.textContent=e.message;});
    }
    form.querySelectorAll('[data-song-drive-field] input[name="drive_thumbnail_folder"]').forEach(x=>x.required=useDrive);
    flip('[data-song-sheet-field]',useSheet,'metadata_sheet_url_or_id');
    const tab=form.querySelector('input[name="metadata_sheet_tab"]');
    if(tab)tab.required=useSheet&&!!form.querySelector('#lum-ae-profile-group');
    flip('[data-song-manual-fields]',!useSheet,'manual_title');
    form.querySelectorAll('[data-song-sheet-note]').forEach(el=>el.hidden=!useSheet);
  }
  document.addEventListener('change',e=>{
    if(e.target?.matches?.('#song-profile-video-source,#song-profile-metadata-source')) applySongProfileSources();
  });
  document.addEventListener('DOMContentLoaded',applySongProfileSources);
  window.addEventListener('pageshow',applySongProfileSources);
  if(document.documentElement){
    new MutationObserver(records=>{
      if(records.some(rec=>[...rec.addedNodes].some(n=>n.nodeType===1 && (n.matches?.('#lum-ae-profile-form')||n.querySelector?.('#lum-ae-profile-form'))))){
        queueMicrotask(applySongProfileSources);
      }
    }).observe(document.documentElement,{childList:true,subtree:true});
  }
  applySongProfileSources();
})();


/* SHORT_GROUP_METADATA_SOURCES_V1 */
(() => {
  function applyShortMetadataSource() {
    const source=document.getElementById('short-profile-metadata-source');
    if(!source)return;
    const form=source.closest('form'), manual=source.value==='manual';
    form.querySelectorAll('[data-short-manual-fields]').forEach(el=>{
      el.hidden=!manual;
      el.querySelector('[name="manual_title"]').required=manual;
    });
    form.querySelectorAll('[data-short-sheet-field]').forEach(el=>{
      el.hidden=manual;
      el.querySelectorAll('[name="metadata_sheet_url_or_id"],[name="metadata_sheet_tab"]').forEach(input=>input.required=!manual);
    });
  }
  document.addEventListener('change',e=>{if(e.target.id==='short-profile-metadata-source')applyShortMetadataSource();});
  document.addEventListener('DOMContentLoaded',applyShortMetadataSource);
  window.addEventListener('pageshow',applyShortMetadataSource);
  new MutationObserver(records=>{
    if(records.some(r=>[...r.addedNodes].some(n=>n.nodeType===1 && (n.id==='lum-ae-profile-form'||n.querySelector?.('#lum-ae-profile-form')))))applyShortMetadataSource();
  }).observe(document.documentElement,{childList:true,subtree:true});
  applyShortMetadataSource();
})();

/* SHORT_STUDIO_DAILY_SCHEDULE_PICKERS: one PKT alarm per daily video, stored using existing CSV. */
(() => {
  'use strict';
  const selectors = '[data-short-schedule-slots]';
  const clock = /^([01]\d|2[0-3]):[0-5]\d$/;
  function scheduleIsEnabled(host) {
    if (host.hasAttribute('data-short-profile-schedule')) {
      const form=host.closest('form');
      return form?.querySelector('[name="work_mode"]')?.value==='auto'
        && !!form.querySelector('[name="schedule_enabled"][type="checkbox"]')?.checked;
    }
    const name=host.dataset.shortScheduleEnabled;
    const row=host.closest('tr');
    return !!row?.querySelector('input[type="checkbox"][name="'+name+'"]')?.checked;
  }
  function countFor(host) {
    const source=host.hasAttribute('data-short-profile-schedule')
      ? host.closest('form')?.querySelector('[name="daily_upload_count"]')?.value
      : host.dataset.shortCount;
    return Math.max(1,Math.min(100,Number.parseInt(source||'4',10)||4));
  }
  function sync(host) {
    const values=[...host.querySelectorAll('input[type="time"]')].map(x=>x.value);
    const active=scheduleIsEnabled(host);
    for (const field of host.querySelectorAll('input[type="time"]')) {
      field.required=active;
      field.setCustomValidity(field.value && values.filter(v=>v===field.value).length>1 ? 'Choose a different time for each video.' : '');
    }
    const hidden=host.parentElement.querySelector('[data-short-schedule-csv]');
    if (hidden) hidden.value=values.filter(v=>clock.test(v)).join(',');
  }
  function build(host,refresh=false) {
    const count=countFor(host);
    if (!refresh && host.dataset.shortReady===String(count)) {sync(host);return;}
    const hidden=host.parentElement.querySelector('[data-short-schedule-csv]');
    if (!hidden) return;
    const existing=[...host.querySelectorAll('input[type="time"]')].map(x=>x.value);
    const saved=(existing.length?existing:(hidden.value||'').split(',')).map(x=>x.trim()).filter(Boolean);
    const fragment=document.createDocumentFragment();
    for(let i=0;i<count;i++){
      const label=document.createElement('label');
      label.className='lum-short-time-slot';
      const title=document.createElement('span');
      title.textContent='Video '+(i+1);
      const input=document.createElement('input');
      input.type='time';input.step='60';input.value=saved[i]||'';
      input.setAttribute('aria-label','Video '+(i+1)+' upload time PKT');
      input.addEventListener('change',()=>sync(host));
      label.append(title,input);fragment.append(label);
    }
    host.replaceChildren(fragment);
    host.dataset.shortReady=String(count);
    sync(host);
  }
  function init() {
    document.querySelectorAll(selectors).forEach(host=>build(host));
  }
  document.addEventListener('input',event=>{
    if(event.target?.matches?.('[name="daily_upload_count"]')){
      document.querySelectorAll('[data-short-profile-schedule]').forEach(host=>build(host,true));
    }
  });
  document.addEventListener('change',event=>{
    if(event.target?.matches?.('[name="schedule_enabled"],[name^="schedule_enabled_"],[name="work_mode"]')) init();
  });
  document.addEventListener('submit',event=>{
    const form=event.target;
    form?.querySelectorAll?.(selectors).forEach(host=>sync(host));
  },true);
  document.addEventListener('DOMContentLoaded',init);
  window.addEventListener('pageshow',init);
  new MutationObserver(records=>{
    if(records.some(r=>[...r.addedNodes].some(n=>n.nodeType===1 && (n.matches?.(selectors)||n.querySelector?.(selectors)))))init();
  }).observe(document.documentElement,{childList:true,subtree:true});
  init();
})();
