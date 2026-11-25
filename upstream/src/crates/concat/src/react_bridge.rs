// SPDX-License-Identifier: AGPL-3.0-or-later
//! React is the visible shell; the existing bound controllers retain ownership.
use std::{cell::RefCell,collections::BTreeMap,sync::{Arc,mpsc}};
use serde_json::{Value,json};
use concat_api::{Api,ApiError,Request,Response};
use crate::host::Shell;
use slint::{Model,ComponentHandle};

thread_local! {
    static API: RefCell<Option<Api>> = const {RefCell::new(None)};
    static IMAGES: RefCell<BTreeMap<String,slint::Image>> = const {RefCell::new(BTreeMap::new())};
}
trait Wire: Sized {
    fn wire(&self,key:&str)->Value;
    fn read(value:&Value)->Result<Self,String>;
}
macro_rules! primitive {
    ($ty:ty) => {impl Wire for $ty {
        fn wire(&self,_:&str)->Value {json!(self)}
        fn read(v:&Value)->Result<Self,String>{serde_json::from_value(v.clone()).map_err(|e|e.to_string())}
    }};
}
primitive!(i32);primitive!(i64);primitive!(f32);primitive!(bool);
impl Wire for () {fn wire(&self,_:&str)->Value{Value::Null}fn read(_:&Value)->Result<Self,String>{Ok(())}}
impl Wire for slint::SharedString {
    fn wire(&self,_:&str)->Value{json!(self.as_str())}
    fn read(v:&Value)->Result<Self,String>{v.as_str().map(Into::into).ok_or("Expected text".into())}
}
impl<T:Wire+Clone+'static> Wire for slint::ModelRc<T> {
    fn wire(&self,key:&str)->Value {Value::Array((0..self.row_count()).filter_map(|i|self.row_data(i).map(|x|x.wire(&format!("{key}.{i}")))).collect())}
    fn read(v:&Value)->Result<Self,String>{let rows=v.as_array().ok_or("Expected list")?.iter().map(T::read).collect::<Result<Vec<_>,_>>()?;Ok(std::rc::Rc::new(slint::VecModel::from(rows)).into())}
}
impl Wire for slint::Color {
    fn wire(&self,_:&str)->Value{json!(format!("#{:02x}{:02x}{:02x}{:02x}",self.red(),self.green(),self.blue(),self.alpha()))}
    fn read(v:&Value)->Result<Self,String>{let text=v.as_str().ok_or("Expected #RRGGBB or #RRGGBBAA")?.trim_start_matches('#');let n=u32::from_str_radix(text,16).map_err(|_|"Invalid color")?;match text.len(){6=>Ok(Self::from_rgb_u8((n>>16) as u8,(n>>8) as u8,n as u8)),8=>Ok(Self::from_argb_u8(n as u8,(n>>24) as u8,(n>>16) as u8,(n>>8) as u8)),_=>Err("Expected #RRGGBB or #RRGGBBAA".into())}}
}
impl Wire for slint::Brush {
    fn wire(&self,key:&str)->Value{self.color().wire(key)}
    fn read(v:&Value)->Result<Self,String>{Ok(slint::Color::read(v)?.into())}
}
impl Wire for slint::Image {
    fn wire(&self,key:&str)->Value{IMAGES.with(|images|images.borrow_mut().insert(key.into(),self.clone()));json!({"$image":key})}
    fn read(v:&Value)->Result<Self,String>{let key=v.get("$image").and_then(Value::as_str).ok_or("Image references are read only")?;IMAGES.with(|images|images.borrow().get(key).cloned()).ok_or("Image expired; refresh state".into())}
}
mod generated { include!("react_wire.rs"); }

fn base64(bytes:&[u8])->String{
    const TABLE:&[u8]=b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out=String::with_capacity(bytes.len().div_ceil(3)*4);
    for chunk in bytes.chunks(3){let n=((chunk[0] as u32)<<16)|((chunk.get(1).copied().unwrap_or(0) as u32)<<8)|(chunk.get(2).copied().unwrap_or(0) as u32);out.push(TABLE[((n>>18)&63) as usize] as char);out.push(TABLE[((n>>12)&63) as usize] as char);out.push(if chunk.len()>1{TABLE[((n>>6)&63) as usize] as char}else{'='});out.push(if chunk.len()>2{TABLE[(n&63) as usize] as char}else{'='});}out
}

fn on_ui<T:Send+'static>(f:impl FnOnce()->Result<T,ApiError>+Send+'static)->Result<T,ApiError>{
    let(tx,rx)=mpsc::sync_channel(1);
    slint::invoke_from_event_loop(move||{let _=tx.send(f());}).map_err(|e|ApiError::failed(e.to_string()))?;
    // Do not time out a mutation then pretend it failed: the stdio caller tracks unknown outcomes.
    rx.recv().map_err(|_|ApiError::failed("Desktop controller stopped"))?
}
fn engine(mut request:Request)->Result<Value,ApiError>{
    let mut result=Err(ApiError::failed("Desktop not ready"));
    Shell::with(|shell,app|{
        let mut studio=shell.studio.borrow_mut();
        studio.flush_commit();
        if studio.session.is_some() { if let Request::EditApply{path,command}=request { request=Request::EditApply{path,command:studio.protect_canvas_tracks(command)}; } }
        let lifecycle=match &request {
            Request::ProjectCreate{location,name,video}=>{
                let v=video.unwrap_or_default();
                Some(concat_host::projects::create(location,name,v.width,v.height,v.rate_num,v.rate_den))
            },
            Request::ProjectOpen{path}=>{
                if studio.session.as_ref().is_some_and(|s|s.path()==path){result=serde_json::to_value(studio.session.as_ref().unwrap().view()).map_err(|e|ApiError::failed(e.to_string()));return;}
                Some(concat_host::projects::open(path))
            },
            Request::TemplateInstantiate{template,location,name,fills}=>{
                Some((||{
                    let fills=fills.iter().map(|fill|Ok(concat_host::templates::SlotFill{media_id:fill.media_id.clone(),item:concat_host::media::probe(&fill.file)?.to_new_media()})).collect::<Result<Vec<_>,String>>()?;
                    concat_host::templates::instantiate(template,location,name,fills)
                })())
            },_=>None,
        };
        if let Some(info)=lifecycle {
            result=(||{let info=info.map_err(ApiError::failed)?;studio.close_project().map_err(ApiError::failed)?;studio.open_project(info).map_err(ApiError::failed)?;serde_json::to_value(studio.session.as_ref().unwrap().view()).map_err(|e|ApiError::failed(e.to_string()))})();
        } else if let Request::ProjectClose{path,..}=&request {
            result=if studio.session.as_ref().is_some_and(|s|s.path()==path){studio.close_project().map(|_|json!({})).map_err(ApiError::failed)}else{Err(ApiError::failed("Project is not open"))};
        } else {
            let changed=matches!(&request,Request::EditApply{..}|Request::EditUndo{..}|Request::EditRedo{..}|Request::MediaImport{..}|Request::ProjectSetVideo{..});
            let saved=matches!(&request,Request::ProjectSave{..});
            API.with(|slot|{
                let mut slot=slot.borrow_mut();let api=slot.as_mut().unwrap();
                let response=if let Some(session)=studio.session.take(){
                    studio.echo=None;
                    let(session,response)=api.dispatch_live(session,request);
                    studio.session=Some(session);response
                }else{api.dispatch(request)};
                result=match response{Response::Result(reply)=>serde_json::to_value(reply).map_err(|e|ApiError::failed(e.to_string())),Response::Error(error)=>Err(error)};
            });
            if changed && result.is_ok(){studio.after_change();}
            if saved && result.is_ok(){studio.react_saved();}
        }
        studio.publish(&app,&shell.models);
    });result
}
struct Desktop;
impl concat_mcp::Backend for Desktop {
    fn call(&mut self,request:Request)->Result<Value,ApiError>{on_ui(move||engine(request))}
    fn describe(&self)->Value{json!({"mode":"react-desktop","source":env!("BUILD_SOURCE_SHA"),"session":"shared"})}
    fn desktop(&mut self,method:&str,params:Value)->Result<Value,ApiError>{
        let method=method.to_owned();on_ui(move||{
            if method=="desktop/schema"{return serde_json::from_str(include_str!("react_schema.json")).map_err(|e|ApiError::failed(e.to_string()));}
            let mut result=Err(ApiError::failed("Desktop not ready"));
            Shell::with(|shell,app|{result=(||{match method.as_str(){
                "desktop/events"=>{
                    let studio=shell.studio.borrow();let(revision,dirty)=studio.react_revision();
                    let editor=app.global::<crate::ui::Editor>();
                    Ok(json!({"path":studio.session.as_ref().map(|s|s.path()),"revision":revision,"dirty":dirty,"toast":app.get_toast().wire("toast"),"playing":editor.get_playing(),"time":editor.get_playhead()}))
                },
                "desktop/playback"=>{let editor=app.global::<crate::ui::Editor>();Ok(json!({"time":editor.get_playhead(),"playing":editor.get_playing()}))},
                "desktop/image"=>{
                    let key=params["key"].as_str().ok_or("Image key required")?;
                    let image=IMAGES.with(|images|images.borrow().get(key).cloned()).ok_or("Image expired; refresh state")?;
                    let pixels=image.to_rgba8().ok_or("This native image is GPU resident; use the program monitor")?;
                    if pixels.width()==0||pixels.height()==0{return Ok(Value::Null);}
                    let mut png=std::io::Cursor::new(Vec::new());
                    let rgba=image::RgbaImage::from_raw(pixels.width(),pixels.height(),pixels.as_bytes().to_vec()).ok_or("Invalid native image")?;
                    image::DynamicImage::ImageRgba8(rgba).write_to(&mut png,image::ImageFormat::Png).map_err(|e|e.to_string())?;
                    Ok(json!(format!("data:image/png;base64,{}",base64(png.get_ref()))))
                },
                "desktop/state"=>Ok(json!({"services":generated::state(&app),"appearance":{"dark":app.global::<crate::ui::Theme>().get_dark(),"scale":app.global::<crate::ui::Theme>().get_ui_scale()},"view":shell.studio.borrow().session.as_ref().map(|s|s.view()),"path":shell.studio.borrow().session.as_ref().map(|s|s.path().to_owned())})),
                "desktop/invoke"=>{
                    let target=params["target"].as_str().ok_or("Target required")?;
                    let callback=params["callback"].as_str().unwrap_or("");
                    // OS window controls belong to WebView2, never the hidden controller window.
                    if target=="App" && ["window-close","window-minimize","window-maximize","quit","titlebar-close","titlebar-minimize","titlebar-toggle-maximize","titlebar-begin-drag"].contains(&callback){return Err("Use the React window controls".into());}
                    let args=params["arguments"].as_array().cloned().unwrap_or_default();
                    let properties: BTreeMap<String,Value>=serde_json::from_value(params.get("properties").cloned().unwrap_or(json!({}))).map_err(|e|e.to_string())?;
                    let before=app.get_toast().token;
                    let value=generated::invoke(&app,target,callback,&args,&properties)?;
                    let notice=app.get_toast();
                    if notice.token!=before && notice.failed{return Err(notice.message.to_string());}
                    Ok(json!({"value":value}))
                },
                _=>Err("Unknown desktop operation".to_owned()),
            }})().map_err(ApiError::failed);});result
        })
    }
    fn finish(&mut self){let _=on_ui(||{let mut result=Ok(());Shell::with(|shell,_|{result=shell.studio.borrow_mut().close_project().map_err(ApiError::failed)});API.with(|slot|{if let Some(api)=slot.borrow_mut().as_mut(){api.stopper().cancel_all();api.finish();}});slint::quit_event_loop().map_err(|e|ApiError::failed(e.to_string()))?;result});}
}
pub fn start()->Result<(),slint::PlatformError>{
    let mut api=Api::new(Arc::new(|_|{})).map_err(slint::PlatformError::Other)?;
    Shell::with(|shell,_|api.share_exporter(shell.studio.borrow().host.exporter.clone()));
    API.with(|slot|*slot.borrow_mut()=Some(api));
    std::thread::Builder::new().name("react-stdio".into()).spawn(||{
        concat_mcp::protocol::serve(std::io::BufReader::new(std::io::stdin()),std::io::stdout(),Box::new(Desktop));
    }).map_err(|e|slint::PlatformError::Other(e.to_string()))?;
    Ok(())
}
