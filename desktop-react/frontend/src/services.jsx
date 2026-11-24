import React,{useState,useEffect,useRef} from 'react';
import {Drawer,Button,Input,InputNumber,Select,Checkbox,Collapse,Space,Alert,Spin,Tag,Empty} from 'antd';
import {call} from './bridge';
const label=s=>s.replace(/[-_]/g,' ').replace(/\b\w/g,x=>x.toUpperCase());
const GROUPS=['Captions','Voiceover','Speech','GPU queue','CBuilder','Settings','Project','Audio library','Animation tools','Other tools'];
const HIDDEN=/^(titlebar-|window-|workspace-resized|dock-|divider-|menu-opened|meter-watched|start-link)/;
function group(target,name){
 if(target==='CloudGpu')return 'GPU queue';
 if(/^(captions|library-apply-caption)/.test(name)||target==='CaptionPresetPreview')return 'Captions';
 if(/^(voiceover|record-voiceover)/.test(name))return 'Voiceover';
 if(/^(speech|speak)/.test(name))return 'Speech';
 if(/^(cb-|c-builder|start-c-builder)/.test(name))return 'CBuilder';
 if(/^(settings|model-|open-settings)/.test(name))return 'Settings';
 if(target==='Sounds')return 'Audio library';
 if(/^(project-|start-|relink-|confirm-|export-)/.test(name))return 'Project';
 if(['CanvasPresets','ParticleBulk','Keyframes','KeyEditor','Backdrop'].includes(target)||/animation|transition|chain-|adjust-|cutout-|enhance|library-/.test(name))return 'Animation tools';
 return 'Other tools';
}
export function defaultValue(type,types){
 if(type.startsWith('['))return [];
 const spec=types[type];if(spec?.kind==='enum')return spec.values[0];
 if(spec?.kind==='struct')return Object.fromEntries(Object.entries(spec.fields).map(([k,t])=>[k,defaultValue(t,types)]));
 if(type==='bool')return false;if(['int','float','length','duration','angle','physical-length'].includes(type))return 0;
 return type==='color'?'#ffffff':'';
}
function Field({type,value,onChange,types,readOnly=false}){
 const spec=types[type];
 if(type==='image')return <Tag>Native image</Tag>;
 if(type==='bool')return <Checkbox checked={!!value} disabled={readOnly} onChange={e=>onChange(e.target.checked)}/>;
 if(['int','float','length','duration','angle','physical-length'].includes(type))return <InputNumber value={value} disabled={readOnly} step={type==='int'?1:.05} precision={type==='int'?0:undefined} onChange={v=>onChange(v??0)}/>;
 if(spec?.kind==='enum')return <Select value={value} disabled={readOnly} options={spec.values.map(v=>({value:v,label:label(v)}))} onChange={onChange} style={{minWidth:160}}/>;
 if(spec?.kind==='struct')return <div className="service-fields">{Object.entries(spec.fields).map(([key,t])=><label key={key}><span>{label(key)}</span><Field type={t} value={value?.[key]??defaultValue(t,types)} onChange={v=>onChange({...value,[key]:v})} types={types} readOnly={readOnly}/></label>)}</div>;
 if(type.startsWith('[')){const t=type.slice(1,-1);return <div>{(value||[]).slice(0,200).map((v,i)=><div key={i} className="service-array"><Field type={t} value={v} onChange={n=>onChange(value.map((x,j)=>j===i?n:x))} types={types} readOnly={readOnly}/>{!readOnly&&<Button size="small" onClick={()=>onChange(value.filter((_,j)=>i!==j))}>Remove</Button>}</div>)}{!readOnly&&<Button onClick={()=>onChange([...(value||[]),defaultValue(t,types)])}>Add item</Button>}</div>}
 return readOnly?<span className="service-text">{typeof value==='string'?value:String(value??'')}</span>:<Input.TextArea autoSize={{minRows:1,maxRows:8}} value={value} onChange={e=>onChange(e.target.value)}/>;
}
function Action({entry,schema,onInvoke}){
 const [args,setArgs]=useState(()=>entry.arguments.map(a=>defaultValue(a.type,schema.types))),[busy,setBusy]=useState(false),[error,setError]=useState('');
 return <div className="service-action"><div className="service-fields">{entry.arguments.map((arg,i)=><label key={arg.name}><span>{label(arg.name)}</span><Field type={arg.type} value={args[i]} onChange={v=>setArgs(args.map((x,j)=>i===j?v:x))} types={schema.types}/></label>)}</div><Button type="primary" loading={busy} onClick={async()=>{setBusy(true);setError('');try{await onInvoke(entry.target,entry.name,args)}catch(e){setError(e.message)}finally{setBusy(false)}}}>{label(entry.name)}</Button>{error&&<Alert type="error" message={error}/>}</div>;
}
export default function Services({open,onClose,initial='Captions',onState,selection=[]}){
 const [schema,setSchema]=useState(null),[native,setNative]=useState(null),[active,setActive]=useState(initial),[search,setSearch]=useState(''),[error,setError]=useState(''),running=useRef(false),last=useRef('');
 async function refresh(){if(running.current)return;running.current=true;try{const v=await call('native_state');setNative(v);const fingerprint=JSON.stringify(v.view);if(last.current!==fingerprint){last.current=fingerprint;onState(v.state)}}catch(e){setError(e.message)}finally{running.current=false}}
 async function invoke(target,name,args=[]){await call('native_invoke',target,name,args);await refresh()}
 useEffect(()=>{if(!open)return;setActive(initial);setError('');let alive=true;(async()=>{try{const s=await call('native_schema');if(!alive)return;setSchema(s);if(selection.length)await call('select_native',selection);await refresh()}catch(e){setError(e.message)}})();const timer=setInterval(refresh,1000);return()=>{alive=false;clearInterval(timer)}},[open,initial]);
 const entries=schema?Object.entries(schema.targets).flatMap(([target,t])=>t.callbacks.map(c=>({...c,target}))).filter(c=>!HIDDEN.test(c.name)&&group(c.target,c.name)===active&&(`${c.name} ${c.target}`).toLowerCase().includes(search.toLowerCase())):[];
 const modelNames={Captions:['App.captions','CaptionPresetPreview.cards'],Voiceover:['App.voiceover'],Speech:['App.speech','App.voices'],'GPU queue':['CloudGpu.queue','CloudGpu.rows','CloudGpu.status','CloudGpu.error'],CBuilder:['Editor.cb-keywords','Editor.cb-status','App.cb-generating','App.cb-generated-project'],Settings:['App.settings','App.transcribers','App.system-facts'],Project:['App.project-sheet','App.export','App.relink','App.confirm'],'Audio library':['Sounds.voices','Sounds.sounds']};
 const models=(modelNames[active]||[]).flatMap(path=>{const [t,k]=path.split('.');const value=native?.services?.[t]?.[k];return value===undefined?[]:[{key:path,label:label(k),children:<Field type={schema?.targets[t]?.properties[k]} value={value} onChange={()=>{}} types={schema?.types||{}} readOnly/>}]});
 return <Drawer title="Concat tools" width="min(1000px, 95vw)" open={open} onClose={onClose}><div className="service-nav"><Select value={active} onChange={setActive} options={GROUPS.map(value=>({value}))}/><Input.Search placeholder="Find a control" value={search} onChange={e=>setSearch(e.target.value)}/><Button onClick={refresh}>Refresh</Button></div>{error&&<Alert type="error" message={error}/>}<p className="hint">These controls call the existing Concat services in your current project. Downloads, progress, cancellation and settings use the native controllers.</p>{!schema?<Spin/>:<><Space wrap>{[['Captions','Editor','captions'],['Voiceover','Editor','record-voiceover'],['Speech','Editor','speak'],['Settings','App','open-settings'],['Project','Editor','modify-project']].filter(x=>x[0]===active).map(([text,t,c])=><Button key={c} onClick={()=>invoke(t,c).catch(e=>setError(e.message))}>Open {text}</Button>)}</Space><Collapse items={models} style={{margin:'16px 0'}}/><Collapse accordion items={entries.map(c=>({key:c.target+'.'+c.name,label:label(c.name),children:<Action key={c.target+'.'+c.name} entry={c} schema={schema} onInvoke={invoke}/>}))}/>{!entries.length&&<Empty description="No matching controls"/>}</>}</Drawer>
}
