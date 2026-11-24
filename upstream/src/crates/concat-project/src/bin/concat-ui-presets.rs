// SPDX-License-Identifier: AGPL-3.0-or-later
//! React desktop adapter: reuse native generators, ownership and compact recipes.
use concat_project::{Command, model::Clip, motion_preset::{self,MotionPreset}, physics_motion::{PhysicsControls,PhysicsPreset}, canvas_motion::{CanvasMotionControls,CanvasMotionPreset}};
use serde_json::{Value,json};
const BASIC:[MotionPreset;11]=[MotionPreset::PushIn,MotionPreset::PanRight,MotionPreset::FadeIn,MotionPreset::PushOut,MotionPreset::PanLeft,MotionPreset::PanUp,MotionPreset::PanDown,MotionPreset::FadeOut,MotionPreset::RotateLeft,MotionPreset::RotateRight,MotionPreset::OpacityPulse];
fn controls<T:serde::Serialize+serde::de::DeserializeOwned>(default:T,patch:&Value)->Result<T,String>{
 let mut value=serde_json::to_value(default).map_err(|e|e.to_string())?;
 if let Some(map)=patch.as_object(){for(k,v)in map{if value.get(k).is_none(){return Err(format!("Unknown control: {k}"));}value[k]=v.clone();}}
 serde_json::from_value(value).map_err(|e|e.to_string())
}
fn run(v:Value)->Result<Value,String>{
 if v["operation"]=="catalogue"{
  let mut rows=Vec::new();
  for (i,p) in BASIC.into_iter().enumerate(){rows.push(json!({"id":p.id(),"category":"basic","card":format!("animation-card-{i}.svg")}));}
  for (i,p) in PhysicsPreset::ALL.into_iter().enumerate(){rows.push(json!({"id":p.id(),"category":"physics","card":format!("animation-card-{}.svg",i+11)}));}
  for p in CanvasMotionPreset::ALL{rows.push(json!({"id":p.id(),"category":"canvas"}));}
  for i in concat_project::canvas_preset::ACTIVE_PRESETS{rows.push(json!({"id":format!("canvas-preset.{i}"),"category":"adjustment"}));}
  return Ok(json!({"presets":rows,"defaults":{"basic":PhysicsControls::default(),"physics":PhysicsControls::default(),"canvas":CanvasMotionControls::default()}}));
 }
 let clip:Clip=serde_json::from_value(v["clip"].clone()).map_err(|e|e.to_string())?;
 let id=v["preset"].as_str().ok_or("Preset required")?;
 if id=="none"{return serde_json::to_value(Command::ClearGeneratedMotion{clip_id:clip.id,expected_keys:clip.keys}).map_err(|e|e.to_string());}
 if let Some(number)=id.strip_prefix("canvas-preset."){
  let number:u32=number.parse().map_err(|_|"Invalid canvas preset")?;
  let config=concat_project::canvas_preset::Config::preset(number).ok_or("Canvas preset unavailable")?;
  let config=controls(config,&v["controls"])?;
  return serde_json::to_value(Command::ApplyCanvasPreset{config,targets:vec![clip.id],locked_tracks:vec![]}).map_err(|e|e.to_string());
 }
 let frame=v["frame"].as_f64().ok_or("Frame duration required")?;
 let base=motion_preset::preset_base(&clip,std::iter::empty::<&[concat_project::model::ClipKey]>()).ok_or("This clip has manual keys. Clear or edit those in the advanced editor first.")?;
 let (keys,style,recipe)=if let Some(p)=BASIC.into_iter().find(|p|p.id()==id){
  let c:PhysicsControls=controls(PhysicsControls::default(),&v["controls"])?;if !c.valid(){return Err("Invalid motion controls".into());}
  let keys=p.keys_with(&base,frame,c.strength,c.duration,c.speed).map_err(|e|format!("{e:?}"))?.0;
  (keys,None,concat_project::motion_wire::recipe(frame,c))
 }else if let Some(p)=PhysicsPreset::ALL.into_iter().find(|p|p.id()==id){
  let c:PhysicsControls=controls(PhysicsControls::default(),&v["controls"])?;if !c.valid(){return Err("Invalid physics controls".into());}
  let keys=p.keys_with(&base,frame,c).map_err(|e|format!("{e:?}"))?.0;
  (keys,None,concat_project::motion_wire::recipe(frame,c))
 }else{
  let p=CanvasMotionPreset::ALL.into_iter().find(|p|p.id()==id).ok_or("Unknown native preset")?;
  let c:CanvasMotionControls=controls(CanvasMotionControls::default(),&v["controls"])?;if !c.valid(){return Err("Invalid canvas controls".into());}
  let keys=p.keys(&base,frame,c).map_err(|e|format!("{e:?}"))?;
  (keys,Some((c.blur,None)),concat_project::motion_wire::recipe(frame,c))
 };
 serde_json::to_value(Command::ApplyGeneratedMotion{clip_id:clip.id,expected_keys:clip.keys,keys,preset:id.into(),canvas_style:style,recipe:Some(recipe)}).map_err(|e|e.to_string())
}
fn main(){let result=serde_json::from_reader(std::io::stdin().lock()).map_err(|e|e.to_string()).and_then(run);let reply=match result{Ok(data)=>json!({"ok":true,"data":data}),Err(error)=>json!({"ok":false,"error":error})};if serde_json::to_writer(std::io::stdout().lock(),&reply).is_err(){std::process::exit(1);}}
