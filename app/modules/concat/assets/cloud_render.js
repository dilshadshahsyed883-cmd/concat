/* Admin-only Production queue, Media and GPU Configuration */
(() => {
  "use strict";
  const root = document.getElementById("cloud-render");
  if (!root) return;
  const csrf = root.dataset.csrf;
  const $ = id => document.getElementById(id);
  const msg = (text, error=false) => { $('cr-message').textContent = text; $('cr-message').dataset.error = error ? "1" : "0"; };
  const api = async (path, options={}) => {
    const headers = { "X-Cloud-Render-CSRF": csrf };
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch("/api/cloud-render" + path, {credentials:"same-origin", ...options, headers:{...headers,...(options.headers||{})}});
    const type = response.headers.get("content-type")||"";
    const result = type.includes("application/json") ? await response.json() : {detail:"HTTP "+response.status};
    if (!response.ok) throw Error(typeof result.detail === "string" ? result.detail : "Request failed ("+response.status+")");
    return result;
  };
  const send = (path, body={}) => api(path,{method:"POST",body:JSON.stringify(body)});
  const fmt = n => {
    const num=Number(n||0);
    return num>=1e9?(num/1e9).toFixed(2)+" GB":num>=1e6?(num/1e6).toFixed(1)+" MB":Math.round(num/1024)+" KB";
  };
  const button = (label, action, variant="secondary") => {
    const b = document.createElement("button");
    b.type="button"; b.className="cr-btn "+variant; b.textContent=label;
    b.addEventListener("click",action);
    return b;
  };
  // Copy only an authenticated same-origin media reference; never embed a VPS path or token.
  const copyMediaLink = async id => {
    const url = new URL("/api/cloud-render/media/" + encodeURIComponent(id) + "/file", window.location.origin).href;
    try {
      if (!navigator.clipboard || !navigator.clipboard.writeText) {
        throw new Error("Clipboard unavailable. Open this page over HTTPS and allow clipboard access.");
      }
      await navigator.clipboard.writeText(url);
      msg("Video link copied. Paste it into Concat Uploader (sign-in required).");
    } catch (error) {
      msg("Could not copy video link: " + (error.message || "clipboard permission denied"), true);
    }
  };
  let configured=false;
  let config={};
  const workerFields = {image_pull_threshold:"cr-pull-threshold", image_pull_seconds:"cr-pull-seconds"};
  const groupFields = {salad_organization:"cr-salad-organization", salad_project:"cr-salad-project", salad_container_group:"cr-salad-container-group"};
  const updateAutomation=()=>{
    const threshold=$("cr-pull-threshold"), seconds=$("cr-pull-seconds");
    $("cr-health-rule").textContent="Keep the instance if Docker image download reaches "+threshold.value+"% within "+seconds.value+" seconds; otherwise reallocate, up to 3 attempts per cycle. If all fail, wait 5 minutes and retry automatically.";
    for(const [input,minus,plus] of [[threshold,"cr-threshold-minus","cr-threshold-plus"],[seconds,"cr-timer-minus","cr-timer-plus"]]){
      $(minus).disabled=Number(input.value)<=Number(input.min);
      $(plus).disabled=Number(input.value)>=Number(input.max);
    }
    const automatic=$("cr-auto-start").value==="automatic";
    $("cr-manual-start").disabled=automatic;
    $("cr-manual-stop").disabled=automatic;
  };
  const stepInput=(id,step)=>{
    const input=$(id);
    input.value=String(Math.min(Number(input.max),Math.max(Number(input.min),Number(input.value||input.min)+step)));
    updateAutomation();
  };
  const workerLabel=worker=>(worker.salad_worker_status||"disabled").replaceAll("_"," ")
    +(worker.salad_worker_progress==null?"":" · image "+worker.salad_worker_progress+"%")
    +" · reallocations "+(worker.salad_reallocations||0)+"/3";
  async function loadConfig(){
    config = await api("/config");
    configured=true;
    $("cr-auto").checked=!!config.auto_upload;
    $("cr-endpoint").value=config.endpoint||"";
    $("cr-concurrency").value=String(config.concurrency||1);
    $("cr-auto-start").value=config.auto_start?"automatic":"manual";
    for(const [key,id] of Object.entries(workerFields)) $(id).value=String(config[key]);
    for(const [key,id] of Object.entries(groupFields)) $(id).value=config[key]||"";
    $("cr-connection-setup").open=!(config.endpoint&&config.salad_key_saved&&config.worker_key_saved&&config.auto_shutdown_ready);
    $("cr-automation-status").textContent=workerLabel(config);
    updateAutomation();
    $("cr-concurrency-status").textContent = "Saved limit: " + (config.concurrency||1) + " · synchronized on connection/dispatch";
    $("cr-salad-key-state").textContent=config.salad_key_saved?"Saved":"Not saved";
    $("cr-worker-key-state").textContent=config.worker_key_saved?"Saved":"Not saved";
    const online=!!config.enabled;
    $("cr-health").textContent=online?"Worker enabled":"Worker not connected";
    $("cr-connection").textContent=online?"Enabled — verify worker":"Image not GPU-verified";
  }
  const operations = async (label,callback) => {
    try {msg(label+"..."); await callback();msg(label+" successful");await refresh();}
    catch(e){msg(e.message,true);}
  };
  const refresh = async()=>{
    const data=await api("/media");
    $("cr-count").textContent=data.items.length;
    $("cr-ready").textContent=data.items.filter(x=>["ready","failed"].includes(x.state)).length;
    $("cr-delivered").textContent=data.items.filter(x=>x.state==="delivered").length;
    const list=$("cr-media-list");
    list.replaceChildren();
    if(!data.items.length){const p=document.createElement("p");p.textContent="No rendered MP4 files received yet.";list.appendChild(p);return;}
    for(const file of data.items){
      const card=document.createElement("article");card.className="cr-media-item";
      const head=document.createElement("div");head.className="cr-media-head";
      const name=document.createElement("strong");name.textContent=file.name;
      const sub=document.createElement("small");
      sub.textContent=fmt(file.size)+" · "+file.state+" · "+(file.drive_path?"Google Drive verified":"VPS");
      head.append(name,sub);card.appendChild(head);
      if(file.error){const warn=document.createElement("p");warn.className="cr-error";warn.textContent=file.error;card.appendChild(warn);}
      const actions=document.createElement("div");actions.className="cr-actions";
      const busy=["queued","uploading","receiving","verified"].includes(file.state);
      const local=["ready","failed","queued","uploading"].includes(file.state);
      // The existing /file route only serves local, verified files in these states.
      // Drive-delivered records no longer have a VPS copy, so never advertise a dead link.
      if(["ready","failed","queued"].includes(file.state)){
        actions.append(button("Copy Link",()=>copyMediaLink(file.id)));
      }
      if(local){
        actions.append(button("Preview",()=>window.open("/api/cloud-render/media/"+encodeURIComponent(file.id)+"/file","_blank","noopener,noreferrer")));
        actions.append(button("Download",()=>{window.location.href="/api/cloud-render/media/"+encodeURIComponent(file.id)+"/file?download=1";}));
      }
      if(["ready","failed"].includes(file.state)){
        actions.append(button(file.state==="failed"?"Retry Drive Upload":"Upload to Drive",
          ()=>operations("Drive upload queued",()=>send("/media/"+encodeURIComponent(file.id)+"/drive")),"primary"));
        actions.append(button("Rename",async()=>{
          const name=window.prompt("New MP4 filename",file.name);
          if(name===null)return;
          await operations("Rename",()=>send("/media/"+encodeURIComponent(file.id)+"/rename",{name}));
        }));
      }
      const remove=button("Delete",async()=>{
        if(!window.confirm("Permanently delete "+file.name+" from VPS? This does not delete a Drive copy."))return;
        await operations("Delete",()=>api("/media/"+encodeURIComponent(file.id),{method:"DELETE"}));
      },"danger");
      remove.disabled=busy;
      actions.append(remove);card.appendChild(actions);list.append(card);
    }
  };
  let previousQueue = "";
  $('cr-report-hours').addEventListener('change', () => {
    const hours = $('cr-report-hours').value;
    if (['1','24','168'].includes(hours)) $('cr-download-report').href = '/api/cloud-render/metrics/report?hours=' + hours;
  });
  let queueRequest = 0;
  async function loadProduction(){
    const request = ++queueRequest;
    const data = await api("/production");
    if (request !== queueRequest) return;
    $("cr-queue-state").textContent =
      ({stopped:"Stopped", running:"Running", paused:"Paused"}[data.state] || data.state)
      + " · Rendering " + (data.rendering ?? data.active) + "/" + data.concurrency
      + " · Uploading " + (data.uploading || 0)
      + " · GPU Ready " + (data.gpu_ready || 0)
      + " · Delivering " + (data.delivering || 0)
      + " · Drive " + (data.intake_active || 0) + " downloading/verifying"
      + " · VPS Ready " + (data.ready_for_gpu || 0);
    const worker=data.worker||{};
    $("cr-auto-worker-status").textContent=(worker.auto_start?"Automatic · ":"Manual · ")+workerLabel(worker)
      +" · shutdown "+(worker.salad_idle_status||"waiting").replaceAll("_"," ");
    $("cr-automation-status").textContent=workerLabel(worker);
    for (const [action, state] of [["start","stopped"],["pause","running"],["resume","paused"]]) {
      $("cr-"+action+"-queue").disabled = data.state !== state;
    }
    const key = JSON.stringify(data.items) + (data.items.some(item => item.state === "Retrying Drive")
      ? Math.floor(Date.now() / 1000) : "");
    if (key === previousQueue) return;
    previousQueue = key;
    const list = $("cr-production-list");
    list.replaceChildren();
    if (!data.items.length) {
      const empty = document.createElement("p");
      empty.textContent = "No Concat projects queued.";
      list.append(empty);
    }
    for (const item of data.items) {
      const card = document.createElement("article");
      card.className = "cr-media-item";
      const head = document.createElement("div");
      head.className = "cr-media-head";
      const name = document.createElement("strong");
      name.textContent = item.name;
      const status = document.createElement("small");
      status.textContent = item.state + (item.cancel_requested && item.state !== "Cancelled" ? " · Cancel requested" : "");
      if (item.state === "Retrying Drive") {
        const seconds = Math.max(0, Math.ceil(item.intake_retry_at - Date.now() / 1000));
        status.textContent = "Drive retry " + item.intake_retries + "/5 · next attempt in " + seconds + "s";
      }
      head.append(name, status);
      card.append(head);
      if (item.error) {
        const error = document.createElement("p");
        error.className = "cr-error";
        error.textContent = item.error;
        card.append(error);
      }
      const actions = document.createElement("div");
      actions.className = "cr-actions";
      const mutate = (action, label) => productionAction("/jobs/"+item.id+"/"+action, label);
      const terminal = ["Completed", "Failed", "Cancelled"].includes(item.state);
      if (!terminal && !item.cancel_requested) {
        actions.append(button("Cancel", () => mutate("cancel", "Cancellation requested")));
      }
      if (item.state === "Failed") {
        actions.append(button("Retry", () => mutate("retry", "Project queued"), "primary"));
      }
      if (terminal || item.state === "Queued") {
        actions.append(button("Delete", () => {
          if (window.confirm("Delete this Production queue entry? The original Drive ZIP and any delivered MP4 are kept.")) {
            mutate("delete", "Queue entry deleted");
          }
        }, "danger"));
      }
      card.append(actions);
      list.append(card);
    }
  }
  async function productionAction(path, label){
    try {
      await send("/production"+path);
      msg(label);
      await loadProduction();
    } catch (e) {
      msg(e.message, true);
    }
  }
  for (const [action, label] of [["start","Queue started"],["pause","Queue paused"],["resume","Queue resumed"]]) {
    $("cr-"+action+"-queue").onclick = () => productionAction("/queue/"+action, label);
  }
  let savedDriveLink = '', driveBusy = false, sourceLoading = false, sourceSaving = false;
  const driveDialog = $('cr-drive-source-dialog');
  const updateDriveControls = () => {
    $('cr-scan').disabled = driveBusy || sourceSaving;
    $('cr-edit-drive-source').disabled = driveBusy || sourceLoading || sourceSaving;
    $('cr-sync-drive').disabled = driveBusy || sourceLoading || sourceSaving || !savedDriveLink;
    $('cr-save-drive-source').disabled = driveBusy || sourceLoading || sourceSaving;
  };
  async function loadDriveSource() {
    sourceLoading = true;
    updateDriveControls();
    try {
      const source = await api('/production/drive-source');
      savedDriveLink = typeof source.link === 'string' ? source.link : '';
      $('cr-saved-drive-link').value = savedDriveLink;
      $('cr-drive-source-state').textContent = savedDriveLink
        ? 'Saved Drive link ready. Click Sync Drive to scan for new ZIPs.'
        : 'Save a Drive link to enable Sync Drive.';
    } finally {
      sourceLoading = false;
      updateDriveControls();
    }
  }
  $('cr-edit-drive-source').onclick = async () => {
    if (driveBusy || sourceSaving || driveDialog.open) return;
    $('cr-drive-source-message').textContent = 'Loading saved link…';
    $('cr-saved-drive-link').disabled = true;
    driveDialog.showModal();
    try {
      await loadDriveSource();
      $('cr-drive-source-message').textContent = '';
    } catch (error) {
      $('cr-drive-source-message').textContent = error.message;
    } finally {
      $('cr-saved-drive-link').disabled = false;
      if (driveDialog.open) $('cr-saved-drive-link').focus();
    }
  };
  $('cr-close-drive-source').onclick = () => { driveDialog.close(); $('cr-edit-drive-source').focus(); };
  $('cr-drive-source-form').onsubmit = async event => {
    event.preventDefault();
    if (sourceSaving || sourceLoading || driveBusy) return;
    sourceSaving = true;
    $('cr-save-drive-source').textContent = 'Saving…';
    updateDriveControls();
    try {
      const source = await send('/production/drive-source', {link: $('cr-saved-drive-link').value.trim()});
      savedDriveLink = source.link;
      $('cr-drive-source-state').textContent = 'Drive link saved. Click Sync Drive to scan for new ZIPs.';
      $('cr-drive-source-message').textContent = '';
      driveDialog.close();
      $('cr-edit-drive-source').focus();
    } catch (error) {
      $('cr-drive-source-message').textContent = error.message;
    } finally {
      sourceSaving = false;
      $('cr-save-drive-source').textContent = 'Save Drive Link';
      updateDriveControls();
      if (!driveDialog.open) $('cr-edit-drive-source').focus();
    }
  };
  async function scanDrive(saved) {
    if (driveBusy || (saved && !savedDriveLink)) return;
    driveBusy = true;
    const trigger = $(saved ? 'cr-sync-drive' : 'cr-scan');
    const label = trigger.textContent;
    trigger.textContent = saved ? 'Syncing Drive…' : 'Scanning Drive…';
    trigger.setAttribute('aria-busy', 'true');
    updateDriveControls();
    $('cr-drive-source-state').textContent = saved ? 'Syncing saved Drive link…' : 'Scanning Drive link…';
    try {
      msg("Scanning Drive…");
      const result = await send(saved ? '/production/sync-drive' : '/production/scan',
        saved ? {} : {link: $('cr-drive-link').value.trim()});
      const summary = result.added + ' added · ' + result.duplicates + ' duplicates · ' + (result.failed || 0) + ' failed';
      const text = (result.added || result.duplicates) ? summary : 'No ZIP files found. ' + summary;
      msg(text);
      $('cr-drive-source-state').textContent = text;
      if (!saved) $('cr-drive-link').value = '';
      await loadProduction().catch(() => msg(text + ' · Queue refresh failed; refresh the page.', true));
    } catch (e) {
      msg(e.message, true);
      $('cr-drive-source-state').textContent = 'Drive scan failed: ' + e.message;
    } finally {
      driveBusy = false;
      trigger.textContent = label;
      trigger.setAttribute('aria-busy', 'false');
      updateDriveControls();
    }
  }
  $('cr-production-form').onsubmit = event => {
    event.preventDefault();
    return scanDrive(false);
  };
  $('cr-sync-drive').onclick = () => scanDrive(true);
  document.querySelectorAll(".cr-tabs button").forEach(b=>b.addEventListener("click",()=>{
    document.querySelectorAll(".cr-tabs button").forEach(x=>{
      const active=x===b;x.classList.toggle("active",active);x.setAttribute("aria-selected",String(active));
    });
    for (const tab of ["production","media","gpu"]) $("cr-"+tab).hidden = b.dataset.tab !== tab;
    if (b.dataset.tab === "production") loadProduction().catch(e=>msg(e.message,true));
    if (b.dataset.tab === "media") refresh().catch(e=>msg(e.message,true));
  }));
  $("cr-refresh").onclick=()=>refresh().catch(e=>msg(e.message,true));
  $("cr-save-auto").onclick=()=>operations("Preference saved",async()=>{
    await send("/config",{auto_upload:$("cr-auto").checked});await loadConfig();
  });
  $("cr-auto-start").addEventListener("change",updateAutomation);
  for(const id of Object.values(workerFields)) $(id).addEventListener("input",updateAutomation);
  for(const [button,input,step] of [["cr-threshold-minus","cr-pull-threshold",-1],["cr-threshold-plus","cr-pull-threshold",1],["cr-timer-minus","cr-pull-seconds",-1],["cr-timer-plus","cr-pull-seconds",1]]) $(button).onclick=()=>stepInput(input,step);
  $("cr-save-gpu").onclick=()=>operations("GPU config saved",async()=>{
    const obj={endpoint:$("cr-endpoint").value.trim(),concurrency:Number($("cr-concurrency").value)};
    for(const [key,id] of Object.entries(groupFields)) obj[key]=$(id).value.trim();
    if($("cr-salad-key").value) obj.salad_key=$("cr-salad-key").value;
    if($("cr-worker-key").value) obj.worker_key=$("cr-worker-key").value;
    const saved=await send("/config",obj);
    $("cr-salad-key").value="";$("cr-worker-key").value="";
    await loadConfig();
    $("cr-concurrency-status").textContent=saved.concurrency_sync==="applied"?"Applied to connected worker":"Saved · will apply when the worker is connected or the queue runs";
  });
  $("cr-save-automation").onclick=()=>operations("Automation settings saved",async()=>{
    const obj={auto_start:$("cr-auto-start").value==="automatic"};
    for(const [key,id] of Object.entries(workerFields)){
      const input=$(id),value=Number(input.value);
      if(!Number.isInteger(value)||value<Number(input.min)||value>Number(input.max)) throw Error("Enter a whole number from "+input.min+" to "+input.max);
      obj[key]=value;
    }
    await send("/config",obj);
    await loadConfig();
    await loadProduction();
  });
  for(const action of ["start","stop"]) $("cr-manual-"+action).onclick=()=>operations("Worker "+action+" queued",async()=>{
    await send("/gpu/worker/"+action);
    await loadProduction();
  });
  $("cr-auto-retry").onclick=async()=>{
    if(!window.confirm("Reset worker action retries? Check Salad first if a start, stop or reallocation is unconfirmed."))return;
    await operations("Worker action retry",()=>send("/gpu/auto-retry"));
  };
  $("cr-test").onclick=async()=>{
    try {
      msg("Testing worker connection...");
      const result=await send("/gpu/test");
      msg(result.ok?"GPU worker reachable":"GPU worker unavailable: "+result.status,!result.ok);
    }catch(e){msg(e.message,true);}
  };
  $("cr-connect").onclick=()=>operations("Worker connection",async()=>{
    const result=await send("/gpu/connect");if(!result.ok)throw Error("Worker "+result.status);
    await loadConfig();
  });
  $("cr-disconnect").onclick=()=>operations("Worker disconnected",async()=>{
    await send("/gpu/disconnect");await loadConfig();
  });
  $("cr-rotate").onclick=async()=>{
    if(!window.confirm("Generate a new receiver token? Existing Salad workers will need the new token."))return;
    try {const result=await send("/token/rotate");$("cr-token").hidden=false;
      $("cr-token").textContent=result.receiver_token;msg("New receiver token shown once. Copy securely to Salad worker.");
    }catch(e){msg(e.message,true);}
  };
  const initial = root.dataset.initialTab || "production";
  if (initial !== "production") {
    const chosen = Array.from(document.querySelectorAll(".cr-tabs button")).find(x => x.dataset.tab === initial);
    if (chosen) chosen.click();
  }
  Promise.all([loadConfig(),loadProduction()]).catch(e=>msg(e.message,true));
  loadDriveSource().catch(() => { $('cr-drive-source-state').textContent = 'Could not load the saved Drive link. Use Save / Edit Drive Link to retry.'; });
  // Refresh state labels while Production is visible.
  window.setInterval(()=>{if(!document.hidden&&(!$("cr-production").hidden||!$("cr-gpu").hidden))loadProduction().catch(()=>{});},5000);
  // Refresh the visible list to surface queued/verified delivery states.
  window.setInterval(()=>{if(configured&&!document.hidden&&!$("cr-media").hidden)refresh().catch(()=>{});},12000);
})();
