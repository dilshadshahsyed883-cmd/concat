// SPDX-License-Identifier: AGPL-3.0-or-later
//! A first-class Home workflow sharing the existing builder state, without a dummy project.
use super::*;
impl Studio {
    pub fn builder_home(&mut self) {
        if self.c_builder.scanning {return;}
        if self.session.is_some() && self.close_project().is_err() {return;}
        self.on_start=true;self.cb_home=true;
    }
    pub fn builder_return_home(&mut self,profile:&str) {
        if self.c_builder.scanning {return;}
        self.c_builder.profile_json=profile.to_owned();
        self.cb_home=false;self.on_start=true;
    }
    pub fn builder_open_generated(&mut self) {
        if self.c_builder.scanning || self.cb_generated_project.is_empty() {return;}
        let path=self.cb_generated_project.clone();
        if self.session.is_some() && self.close_project().is_err() {return;}
        match projects::open(&path).and_then(|info|self.open_project(info)) {
            Ok(())=>{self.cb_home=false;self.on_start=false;}
            Err(error)=>self.notify(&format!("Could not open generated project: {error}"),true),
        }
    }
}

#[derive(Clone)]
pub struct PendingUsed {
    pub library:std::path::PathBuf,
    pub project:std::path::PathBuf,
    pub sources:Vec<std::path::PathBuf>,
    pub cache:std::path::PathBuf,
}
impl Studio {
    pub fn builder_cancel_generation(&mut self) {
        if let Some(cancel)=&self.cb_generation_cancel {
            cancel.store(true,std::sync::atomic::Ordering::Relaxed);
            self.c_builder.status="Cancelling generation; preserving original source files...".into();
        }
    }
    pub fn builder_archive_finished(&mut self,pending:PendingUsed,finish:crate::c_builder_used::Finish) {
        self.c_builder.scanning=false;self.cb_generation_cancel=None;
        self.cb_generated_project=pending.project.to_string_lossy().into_owned();
        match finish.result {
            Ok(report)=>{
                let used:std::collections::HashSet<_>=pending.sources.iter().map(|p|p.to_string_lossy().into_owned()).collect();
                self.c_builder.assets.retain(|a|!used.contains(&a.path));
                if let Some(scan)=finish.refresh {
                    self.c_builder.folder=scan.root.to_string_lossy().into_owned();self.c_builder.keyword_summary=scan.keywords();
                    self.c_builder.assets=scan.assets;self.c_builder.probed=scan.probed;self.c_builder.reused=scan.reused;self.c_builder.removed=scan.removed;
                }
                self.cb_pending_used=None;
                self.c_builder.status=format!("{} media ready in used/ ({} archived now, {} already completed); {} remaining; 0 archival failures. {} original compatibility links retained for older projects.{}",
                    report.entries.len()+report.already_archived,report.entries.len(),report.already_archived,self.c_builder.assets.len(),report.compatibility_links,
                    finish.cache_warning.map(|e|format!(" Cache warning: {e}")).unwrap_or_default());
                self.notify("C Builder project and used media verified. Open in Editor when ready.",false);
            }
            Err(error)=>{
                let cancelled=error.to_ascii_lowercase().contains("cancelled");
                self.c_builder.status=format!("Project retained at {}. Used-media completion is not confirmed; {} cached media entries; {} sources require retry or recovery. {error}",pending.project.display(),self.c_builder.assets.len(),pending.sources.len());
                self.cb_pending_used=if cancelled{None}else{Some(pending)};
                self.notify(&error,true);
            }
        }
    }
    pub fn builder_retry_used(&mut self) {
        if self.c_builder.scanning {return;}
        let Some(pending)=self.cb_pending_used.clone() else{return;};
        let cancel=std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));self.cb_generation_cancel=Some(cancel.clone());
        self.c_builder.scanning=true;self.c_builder.status="Retrying used-media transaction and refreshing inventory...".into();
        let work=pending.clone();
        crate::host::spawn(move||crate::c_builder_used::finish(&work.library,&work.project,&work.sources,&work.cache,&cancel),
            move|studio,_,_,result|studio.builder_archive_finished(pending,result));
    }
}
