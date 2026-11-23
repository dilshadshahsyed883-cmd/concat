/* Offline DOM harness executes the actual production UI script. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../../app/modules/concat/templates/cloud_render.html'), 'utf8');
class Element {
  constructor(id) { this.id=id; this.dataset={}; this.value=''; this.checked=false; this.disabled=false; this.hidden=false; this.textContent=''; this.events={}; this.children=[]; this.classList={toggle(){}}; }
  addEventListener(name, fn) { this.events[name]=fn; }
  setAttribute() {}
  replaceChildren(...nodes) { this.children=nodes; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); }
}
const dom=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(x=>[x[1],new Element(x[1])]));
for(const match of html.matchAll(/<input\s+([^>]+)>/g)){
 const attrs=match[1], id=/id="([^"]+)"/.exec(attrs)?.[1];
 if(!id)continue;
 for(const attr of ['min','max','value'])dom[id][attr]=new RegExp(attr+'="([^"]+)"').exec(attrs)?.[1]||'';
 dom[id].previousElementSibling={textContent:id};
}
dom['cloud-render'].dataset={csrf:'offline-test',initialTab:'production'};
const tabs=['production','media','gpu'].map(tab=>{const element=new Element(tab);element.dataset.tab=tab;return element;});
let config={auto_start:false,auto_shutdown:false,idle_timeout_minutes:5,concurrency:1,image_pull_threshold:8,image_pull_seconds:20,salad_max_reallocations:3,salad_reallocate_cooldown:30};
let saved=null, retryCalls=0;
const context={
 document:{hidden:false,getElementById:id=>dom[id],createElement:tag=>new Element(tag),querySelectorAll:()=>tabs},
 window:{setInterval(){},confirm:()=>true,location:{origin:'https://fixture.example'}}, URL, console,
 fetch:async(url, options={})=>{
  let data={};
  if(url.endsWith('/config')){
   if(options.method==='POST'){saved=JSON.parse(options.body); config={...config,...saved};}
   data=config;
  }else if(url.endsWith('/media'))data={items:[]};
  else if(url.endsWith('/production'))data={state:'running',active:0,concurrency:1,items:[],worker:{...config,salad_worker_status:'checking_download',salad_worker_progress:5,salad_reallocations:0,salad_idle_status:'busy'}};
  else if(url.endsWith('/auto-retry'))retryCalls++;
  else if(url.includes('/gpu/worker/'))data={ok:true};
  return {ok:true,headers:{get:()=> 'application/json'},json:async()=>data};
 }
};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 assert.match(html,/<details class="cr-card" id="cr-connection-setup">/,'connection setup starts collapsed');
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../app/modules/concat/assets/cloud_render.js'),'utf8'),context);
 await flush();
 assert.equal(dom['cr-connection-setup'].open,true,'incomplete setup expands');
 assert.equal(dom['cr-pull-threshold'].value,'8');
 assert.equal(dom['cr-pull-seconds'].value,'20');
 dom['cr-auto-start'].value='automatic';
 dom['cr-auto-start'].events.change();
 assert.equal(dom['cr-manual-start'].disabled,true);
 assert.equal(dom['cr-manual-stop'].disabled,true);
 dom['cr-pull-threshold'].value='12';
 dom['cr-pull-seconds'].value='35';
 await dom['cr-threshold-plus'].onclick();
 assert.equal(dom['cr-pull-threshold'].value,'13');
 await dom['cr-threshold-minus'].onclick();
 await dom['cr-timer-plus'].onclick();
 assert.match(dom['cr-health-rule'].textContent,/12% within 36 seconds/);
 await dom['cr-timer-minus'].onclick();
 dom['cr-salad-organization'].value='org-one';
 await dom['cr-save-automation'].onclick();
 assert.equal(saved.auto_start,true);
 assert.equal(saved.image_pull_threshold,12);
 assert.equal(saved.image_pull_seconds,35);
 assert.match(html,/Automatic shutdown: 1 minute/);
 config={...config,endpoint:'https://test.salad.cloud',salad_key_saved:true,worker_key_saved:true,auto_shutdown_ready:true};
 dom['cr-salad-organization'].value='org-one';
 dom['cr-endpoint'].value=config.endpoint;
 await dom['cr-save-gpu'].onclick();
 assert.equal(saved.salad_organization,'org-one');
 assert.equal(dom['cr-connection-setup'].open,false,'complete setup collapses');
 saved=null;
 dom['cr-pull-threshold'].value='101';
 await dom['cr-save-automation'].onclick();
 assert.equal(saved,null,'invalid input never reaches API');
 assert.equal(dom['cr-message'].dataset.error,'1');
 tabs[0].events.click();
 await flush();
 assert.match(dom['cr-auto-worker-status'].textContent,/checking download.*image 5%.*reallocations 0\/3/);
 dom['cr-auto-start'].value='manual';
 dom['cr-auto-start'].events.change();
 assert.equal(dom['cr-manual-start'].disabled,false);
 await dom['cr-manual-start'].onclick();
 await dom['cr-manual-stop'].onclick();
 await dom['cr-auto-retry'].onclick();
 assert.equal(retryCalls,1);
 assert.match(html,/Download Report \(.txt\)/);
 assert.doesNotMatch(html,/id="cr-resource-dialog"/,'report-only scope has no resource popup');
 dom['cr-report-hours'].value='168';
 dom['cr-report-hours'].events.change();
 assert.equal(dom['cr-download-report'].href,'/api/cloud-render/metrics/report?hours=168');
 dom['cr-report-hours'].value='999';
 dom['cr-report-hours'].events.change();
 assert.equal(dom['cr-download-report'].href,'/api/cloud-render/metrics/report?hours=168');
 console.log('PASS: setup expansion/collapse, automation spinners, rule summary, input persistence/validation, manual controls and API progress');
})().catch(error=>{console.error(error);process.exitCode=1;});
