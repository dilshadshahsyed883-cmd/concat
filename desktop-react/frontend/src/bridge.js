export async function call(method,...args){
 if(typeof window.pywebview?.api?.[method]!=='function')await new Promise((resolve,reject)=>{
   const done=()=>{clearTimeout(timer);resolve()};const timer=setTimeout(()=>{window.removeEventListener('pywebviewready',done);reject(new Error('Desktop bridge is unavailable. Launch Concat-React.exe.'))},15000);
   window.addEventListener('pywebviewready',done,{once:true});
 });
 const result=await window.pywebview.api[method](...args);if(!result.ok)throw new Error(result.error||'Operation failed');return result.data;
}
export const duration=p=>p.scenes.reduce((sum,s)=>sum+s.duration,0);
export function sceneAt(p,t){let start=0;for(let i=0;i<p.scenes.length;i++){if(t<start+p.scenes[i].duration||i===p.scenes.length-1)return {index:i,start,scene:p.scenes[i]};start+=p.scenes[i].duration;}return null}
export const clock=t=>`${Math.floor(Math.max(0,t)/60).toString().padStart(2,'0')}:${(Math.max(0,t)%60).toFixed(2).padStart(5,'0')}`;
