export const CONTRACT = 2;

export function mount(root, context = {}) {
  if (!root) throw new Error('Dashboard root required');
  const lifetime = new AbortController();
  const fetch = (url, options = {}) => window.fetch(url, {...options, credentials: "same-origin", signal:lifetime.signal});
  const $=id=>root.querySelector('#'+id);
  let timer=null, pending=false;
  const preferencesKey = null; // Do not reuse Scene account settings.
  try {
    const saved=preferencesKey && JSON.parse(sessionStorage.getItem(preferencesKey)||'null');
    if(saved && typeof saved.auto==='boolean' && [5,10,30,60].includes(saved.rate)){
      $('dashboard-auto-refresh').checked=saved.auto;
      $('dashboard-refresh-rate').value=String(saved.rate);
    }
  } catch (_) {}
  const fmtGB=v=>Number.isFinite(Number(v))?Number(v).toFixed(1)+' GB':'—';
  function render(data){if (!data || data.ok !== true || !data.vps || !data.vps.disk || !data.vps.ram || !Array.isArray(data.services)) throw new Error("Invalid snapshot");
    const v=data.vps,disk=v.disk,ram=v.ram,svc=data.services,activeSvc=svc.filter(x=>x.active === true).length;
    $('vps-status').textContent=data.ok?'Online':'Attention';$('vps-status-note').textContent=data.ok?'System reachable':'Some checks failed';$('cpu-percent').textContent=(v.cpu_percent??0)+'%';$('cpu-cores').textContent=(v.cpu_cores||0)+' Cores';$('ram-percent').textContent=(ram.percent??0)+'%';$('ram-detail').textContent=`${fmtGB(ram.used_gb)} / ${fmtGB(ram.total_gb)}`;$('disk-percent').textContent=(disk.percent??0)+'%';$('disk-detail').textContent=`${fmtGB(disk.used_gb)} / ${fmtGB(disk.total_gb)}`;$('services-ratio').textContent=`${activeSvc} / ${svc.length}`;$('services-note').textContent=activeSvc===svc.length?'All Core Services Active':'Service attention required';
    const cpu=Number(v.cpu_percent||0),rp=Number(ram.percent||0),dp=Number(disk.percent||0);$('resource-cpu-label').textContent=cpu+'%';$('resource-ram-label').textContent=rp+'%';$('resource-disk-label').textContent=dp+'%';$('resource-cpu-bar').style.width=Math.min(100,cpu)+'%';$('resource-ram-bar').style.width=Math.min(100,rp)+'%';$('resource-disk-bar').style.width=Math.min(100,dp)+'%';$('load-average').textContent=v.load_1m??'—';$('uptime').textContent=v.uptime_human||'—';$('free-storage').textContent=fmtGB(disk.free_gb);



    $('dashboard-updated').textContent='Updated '+(data.generated_at_local||new Date().toLocaleTimeString());
  }
  async function refresh(){if(pending || document.hidden || lifetime.signal.aborted)return;pending=true;const btn=$('dashboard-refresh-now');btn.disabled=true;try{const r=await fetch('/api/v1/dashboard/system',{headers:{Accept:'application/json'},cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);render(await r.json());}catch(e){$('vps-status').textContent='Unknown';$('vps-status-note').textContent='Dashboard API unavailable';$('dashboard-updated').textContent='Refresh failed: '+e.message;}finally{pending=false;btn.disabled=false;}}
  function schedule(){
    if(preferencesKey)try{sessionStorage.setItem(preferencesKey,JSON.stringify({auto:$('dashboard-auto-refresh').checked,rate:Number($('dashboard-refresh-rate').value)}));}catch(_){}
    if(timer)clearInterval(timer);timer=null;if($('dashboard-auto-refresh').checked)timer=setInterval(refresh,Math.max(5,Number($('dashboard-refresh-rate').value||10))*1000)}
  $('dashboard-refresh-now').addEventListener('click',refresh,{signal:lifetime.signal});
  $('dashboard-refresh-rate').addEventListener('change',schedule,{signal:lifetime.signal});
  $('dashboard-auto-refresh').addEventListener('change',schedule,{signal:lifetime.signal});
  refresh();schedule();
  return {unmount(){lifetime.abort();if(timer)clearInterval(timer);timer=null;},dispose(){}};
}
if (!document.getElementById('vps1-shell-outlet')) {
  const root=document.getElementById('control-dashboard');
  if(root){const view=mount(root);window.addEventListener('pagehide',()=>view.unmount(),{once:true});}
}
