// SPDX-License-Identifier: AGPL-3.0-or-later
//! C Builder's isolated, read-only timeline playback. It borrows the app's
//! single audio transport only while Home has no editor session.
use super::*;
use concat_host::preview::{FrameSpec,Monitor};
use std::time::{Duration,Instant};
const WIDTH:u32=854;
const HEIGHT:u32=480;
const FPS:f64=30.0;

pub struct PreviewSession {
    clips:Arc<Vec<concat_export::ExportClip>>,
    monitor:Monitor,
    start:f64,
    end:f64,
    time:f64,
    intro:bool,
    timer:slint::Timer,
    image:slint::Image,
    ready:bool,
    playing:bool,
    muted:bool,
    closing:bool,
    pending:bool,
    last_frame:Option<i64>,
    revision:u64,
    started:Instant,
    status:String,
}

impl Studio {
    pub fn builder_start_preview(&mut self,clips:Arc<Vec<concat_export::ExportClip>>,start:f64,end:f64,intro:bool){
        if !self.cb_home||self.session.is_some()||self.cb_playback_preview.is_some(){self.notify("Preview requires C Builder Home without an editor project",true);return;}
        let audio=audio_specs(&clips);
        if audio.is_empty(){self.notify("Narration is missing from the preview plan",true);return;}
        self.host.playback.pause();
        self.host.playback.set_muted(false);
        // Use the engine's managed PCM cache. No generated project, MP4 or
        // media relocation is involved. Audio is decoded asynchronously.
        let cache=self.host.dirs.data.join("c-builder-preview-audio");
        self.host.playback.set_clips(cache,audio);
        self.host.playback.seek(start);
        self.c_builder.scanning=true;
        let p=PreviewSession{
            clips,monitor:Monitor::isolated(64*1024*1024,2),
            start,end,time:start,intro,timer:slint::Timer::default(),image:Default::default(),
            ready:false,playing:false,muted:false,closing:false,pending:false,last_frame:None,revision:0,
            started:Instant::now(),status:"Preparing narration and first frame...".into(),
        };
        p.timer.start(slint::TimerMode::Repeated,Duration::from_millis(33),||{
            crate::host::Shell::with(|shell,app|{
                let mut studio=shell.studio.borrow_mut();
                let was_open=studio.cb_playback_preview.is_some();
                studio.builder_preview_tick();
                if was_open&&studio.cb_playback_preview.is_none(){studio.publish(&app,&shell.models);}
                else{studio.builder_publish_preview(&app);}
            });
        });
        self.cb_playback_preview=Some(p);
        self.builder_preview_request();
    }
    pub fn builder_preview_toggle(&mut self){
        let Some(p)=self.cb_playback_preview.as_mut() else{return;};
        if !p.ready||p.closing{return;}
        if p.playing{
            p.time=self.host.playback.position_now().clamp(p.start,p.end);
            self.host.playback.pause();p.playing=false;
        }else{
            if p.time>=p.end{p.time=p.start;p.last_frame=None;}
            self.host.playback.play(p.time);p.playing=true;
        }
        self.builder_preview_request();
    }
    pub fn builder_preview_seek(&mut self,seconds:f64){
        let Some(p)=self.cb_playback_preview.as_mut() else{return;};
        if !p.ready||p.closing||!seconds.is_finite(){return;}
        p.time=(p.start+seconds).clamp(p.start,p.end);
        if p.time>=p.end{self.host.playback.pause();p.playing=false;}
        self.host.playback.seek(p.time);p.revision=p.revision.wrapping_add(1);p.last_frame=None;
        self.builder_preview_request();
    }
    pub fn builder_preview_mute(&mut self){
        if let Some(p)=self.cb_playback_preview.as_mut(){if p.ready&&!p.closing{p.muted=!p.muted;self.host.playback.set_muted(p.muted);}}
    }
    pub fn builder_preview_close(&mut self){
        if let Some(p)=self.cb_playback_preview.as_mut(){
            self.host.playback.pause();p.playing=false;p.ready=false;p.closing=true;
            p.status="Closing preview and releasing readers...".into();
        }
        self.builder_preview_tick();
    }
    fn builder_preview_tick(&mut self){
        let Some(p)=self.cb_playback_preview.as_mut() else{return;};
        if p.closing{
            if !p.pending&&!self.host.playback.clips_preparing(){
                let p=self.cb_playback_preview.take().expect("active preview");
                p.timer.stop();drop(p);
                self.host.playback.pause();self.host.playback.set_muted(false);
                self.host.playback.set_clips(self.host.dirs.data.join("c-builder-preview-audio"),Vec::new());
                self.c_builder.scanning=false;
            }
            return;
        }
        if !p.ready{
            if self.host.playback.clips_ready()&&p.last_frame.is_some()&&!p.pending{
                p.ready=true;p.status="Ready · press Play".into();
            }else if p.started.elapsed()>Duration::from_secs(120)||(!self.host.playback.clips_preparing()&&!self.host.playback.clips_ready()){
                p.status="Narration preparation failed; close the preview and check the audio file".into();
                return;
            }
        }
        if p.playing{
            p.time=self.host.playback.position_now().clamp(p.start,p.end);
            if p.time>=p.end{self.host.playback.pause();p.playing=false;}
        }
        self.builder_preview_request();
    }
    fn builder_preview_request(&mut self){
        let Some(p)=self.cb_playback_preview.as_mut() else{return;};
        if p.pending||p.closing{return;}
        let time=p.time.min((p.end-1.0/FPS).max(p.start)).max(p.start);
        let frame=(time*FPS+1e-6).floor() as i64;
        if p.last_frame==Some(frame){return;}
        p.pending=true;
        let monitor=p.monitor.clone();let clips=Arc::clone(&p.clips);let revision=p.revision;
        let spec=FrameSpec{time:((frame as f64/FPS).max(p.start)),width:WIDTH,height:HEIGHT,moving:p.playing,proxy:false,color_space:model::ColorSpace::Sdr};
        crate::host::spawn_detached(move||{
            let settings=concat_project::DocumentSettings{name:"C Builder Preview".into(),width:1920,height:1080,rate_num:30,rate_den:1};
            let result=monitor.frame(clips,&settings,spec);
            // Drop the worker's reader-pool handle before the UI can unblock generation.
            drop(monitor);
            let _=slint::invoke_from_event_loop(move||{
                crate::host::Shell::with(|shell,app|{
                    let mut studio=shell.studio.borrow_mut();
                    if let Some(p)=studio.cb_playback_preview.as_mut(){
                        p.pending=false;
                        if !p.closing&&p.revision==revision{
                            match result{
                                Ok(bytes)=>{
                                    let buffer=slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(&bytes,WIDTH,HEIGHT);
                                    p.image=slint::Image::from_rgba8(buffer);p.last_frame=Some(frame);
                                },
                                Err(e)=>{
                                    p.status=format!("Preview frame failed: {e}");p.ready=false;p.playing=false;p.last_frame=Some(frame);
                                    studio.host.playback.pause();
                                }
                            }
                        }
                    }
                    studio.builder_preview_tick();
                    if studio.cb_playback_preview.is_none(){studio.publish(&app,&shell.models);}
                    else{studio.builder_publish_preview(&app);}
                });
            });
        });
    }
    pub fn builder_publish_preview(&self,app:&App){
        let editor=app.global::<Editor>();
        if let Some(p)=&self.cb_playback_preview{
            editor.set_cb_preview_open(true);
            editor.set_cb_preview_title(if p.intro{"Preview Intro".into()}else{"Preview Video".into()});
            editor.set_cb_preview_ready(p.ready&&!p.closing);
            editor.set_cb_preview_playing(p.playing);
            editor.set_cb_preview_muted(p.muted);
            editor.set_cb_preview_image(p.image.clone());
            editor.set_cb_preview_status(p.status.clone().into());
            editor.set_cb_preview_time((p.time-p.start) as f32);
            editor.set_cb_preview_start(p.start as f32);
            editor.set_cb_preview_duration((p.end-p.start) as f32);
        }else{
            editor.set_cb_preview_open(false);
            editor.set_cb_preview_ready(false);
            editor.set_cb_preview_playing(false);
            editor.set_cb_preview_image(Default::default());
        }
    }
}
