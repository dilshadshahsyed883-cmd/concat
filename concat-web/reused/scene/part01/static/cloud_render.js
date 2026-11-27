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
  async function loadConfig(){
    config = await api("/config");
    configured=true;
    $("cr-auto").checked=!!config.auto_upload;
    $("cr-image").value=config.image||"Not published — cloud_render/concat/gpu/Dockerfile";
    $("cr-endpoint").value=config.endpoint||"";
    $("cr-health-path").value=config.health_path||"/health/ready";
    $("cr-timeout").value=config.timeout||300;
    $("cr-priority").value=config.priority||"high";
    $("cr-concurrency").value=String(config.concurrency||1);
    $("cr-concurrency-status").textContent = "Saved limit: " + (config.concurrency||1) + " · synchronized on connection/dispatch";
    $("cr-salad-key-state").textContent=config.salad_key_saved?"Saved":"Not saved";
    $("cr-worker-key-state").textContent=config.worker_key_saved?"Saved":"Not saved";
    const details=[
      ["Engine",config.engine],["GPU",config.gpu],["VRAM",(config.vram_gb||8)+" GB"],
      ["CPU",config.cpu+" vCPU"],["RAM",config.ram_gb+" GB"],
      ["Storage",config.storage_gb+" GB"],["Replicas",String(config.replicas)],
      ["Architecture",config.architecture],["Port",String(config.port)],
      ["Codec",config.codec],["Concurrency",String(config.concurrency)]
    ];
    const container=$("cr-requirements");container.replaceChildren();
    details.forEach(([title,value])=>{
      const div=document.createElement("div");const span=document.createElement("span");span.textContent=title;
      const strong=document.createElement("strong");strong.textContent=value;div.append(span,strong);container.appendChild(div);
    });
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
  let queueRequest = 0;
  async function loadProduction(){
    const request = ++queueRequest;
    const data = await api("/production");
    if (request !== queueRequest) return;
    $("cr-queue-state").textContent =
      ({stopped:"Stopped", running:"Running", paused:"Paused"}[data.state] || data.state)
      + " · " + data.active + "/" + data.concurrency + " active";
    for (const [action, state] of [["start","stopped"],["pause","running"],["resume","paused"]]) {
      $("cr-"+action+"-queue").disabled = data.state !== state;
    }
    const key = JSON.stringify(data.items);
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
  $("cr-production-form").onsubmit = async event => {
    event.preventDefault();
    const input = $("cr-drive-link");
    const submit = $("cr-scan");
    submit.disabled = true;
    try {
      msg("Scanning Drive…");
      const result = await send("/production/scan", {link:input.value.trim()});
      msg(result.added || result.duplicates
        ? result.added+" added · "+result.duplicates+" already in the queue"
        : "No ZIP files found in this folder.");
      input.value = "";
      await loadProduction();
    } catch (e) {
      msg(e.message, true);
    } finally {
      submit.disabled = false;
    }
  };
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
  $("cr-save-gpu").onclick=()=>operations("GPU config saved",async()=>{
    const obj={endpoint:$("cr-endpoint").value.trim(),health_path:$("cr-health-path").value.trim(),
      timeout:Number($("cr-timeout").value),priority:$("cr-priority").value,
      concurrency:Number($("cr-concurrency").value)};
    if($("cr-salad-key").value) obj.salad_key=$("cr-salad-key").value;
    if($("cr-worker-key").value) obj.worker_key=$("cr-worker-key").value;
    const saved = await send("/config",obj);
    $("cr-salad-key").value="";$("cr-worker-key").value="";
    await loadConfig();
    $("cr-concurrency-status").textContent = saved.concurrency_sync === "applied"
      ? "Applied to connected worker"
      : "Saved · will apply when the worker is connected or the queue runs";
  });
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
  Promise.all([loadConfig(),loadProduction()]).catch(e=>msg(e.message,true));
  // Refresh state labels while Production is visible.
  window.setInterval(()=>{if(!document.hidden&&!$("cr-production").hidden)loadProduction().catch(()=>{});},5000);
  // Refresh the visible list to surface queued/verified delivery states.
  window.setInterval(()=>{if(configured&&!document.hidden&&!$("cr-media").hidden)refresh().catch(()=>{});},12000);
})();