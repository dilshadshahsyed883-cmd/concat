// SPDX-License-Identifier: AGPL-3.0-or-later
//! Transactional project-wise used-media moves with recovery.
#![allow(missing_docs, dead_code)]
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::UNIX_EPOCH;

const JOURNAL: &str = ".concat-cbuilder-used.json";
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Entry {
    pub original: PathBuf,
    pub relocated: PathBuf,
    pub size: u64,
    pub modified_ns: u64,
    pub sha256: String,
    pub project: PathBuf,
}
#[derive(Default, Debug)]
pub struct RelocationReport { pub entries: Vec<Entry>, pub compatibility_links: usize, pub already_archived: usize }
#[derive(Default, Debug)]
pub struct RecoveryReport { pub committed: usize, pub rolled_back: usize }
#[derive(Clone, Serialize, Deserialize)]
struct Pending {
    project: PathBuf,
    before_sha: String,
    after_sha: String,
    entries: Vec<Entry>,
    #[serde(default)] moved: bool,
}
#[derive(Default, Serialize, Deserialize)]
struct Journal {
    #[serde(default)] version: u32,
    #[serde(default)] committed: BTreeMap<PathBuf, Entry>,
    #[serde(default)] pending: Option<Pending>,
}
fn error(e: impl std::fmt::Display) -> String { e.to_string() }
fn stamp(path: &Path) -> Result<(u64,u64),String> {
    let m=fs::symlink_metadata(path).map_err(error)?;
    if !m.is_file() || m.file_type().is_symlink() {return Err(format!("Media must be a regular file: {}",path.display()));}
    let ns=m.modified().map_err(error)?.duration_since(UNIX_EPOCH).map_err(error)?.as_nanos();
    Ok((m.len(),u64::try_from(ns).map_err(error)?))
}
fn hash(path:&Path,cancel:&AtomicBool)->Result<String,String> {
    let mut file=File::open(path).map_err(error)?;let mut digest=Sha256::new();let mut bytes=[0u8;128*1024];
    loop {check_cancel(cancel)?;let n=file.read(&mut bytes).map_err(error)?;if n==0{break;}digest.update(&bytes[..n]);}
    Ok(format!("{:x}",digest.finalize()))
}
fn hash_bytes(bytes:&[u8])->String {format!("{:x}",Sha256::digest(bytes))}
fn check_cancel(cancel:&AtomicBool)->Result<(),String>{if cancel.load(Ordering::Relaxed){Err("C Builder generation cancelled; original media preserved".into())}else{Ok(())}}
fn lock(root:&Path)->Result<File,String>{
    let path=root.join(".concat-cbuilder-used.lock");
    if path.is_symlink(){return Err("Used-media lock cannot be a symlink".into());}
    let file=OpenOptions::new().create(true).truncate(false).read(true).write(true).open(path).map_err(error)?;
    file.try_lock().map_err(|_|"Another C Builder operation is using this library".to_owned())?;Ok(file)
}
fn backup(path:&Path)->PathBuf {path.with_extension("cb-used-backup")}
fn pending_file(path:&Path)->PathBuf {path.with_extension("cb-used-pending")}
/// A Windows-compatible atomic replace; an interrupted replacement has a recoverable backup.
fn replace(path:&Path,bytes:&[u8])->Result<(),String>{
    if path.is_symlink(){return Err(format!("Refusing symbolic-link state: {}",path.display()));}
    let temp=pending_file(path);let saved=backup(path);
    if temp.exists() || saved.exists(){return Err(format!("Recovery state exists for {}",path.display()));}
    let mut f=OpenOptions::new().write(true).create_new(true).open(&temp).map_err(error)?;
    if let Err(e)=f.write_all(bytes).and_then(|_|f.sync_all()){let _=fs::remove_file(&temp);return Err(error(e));}drop(f);
    let existed=path.exists();
    if existed {if let Err(e)=fs::rename(path,&saved){let _=fs::remove_file(&temp);return Err(error(e));}}
    if let Err(e)=fs::rename(&temp,path){if existed{let _=fs::rename(&saved,path);}let _=fs::remove_file(&temp);return Err(error(e));}
    if existed {fs::remove_file(saved).map_err(error)?;}
    #[cfg(unix)] {let _=File::open(path.parent().unwrap_or(Path::new("."))).and_then(|f|f.sync_all());}
    Ok(())
}
fn journal(root:&Path)->Result<Journal,String>{
    let path=root.join(JOURNAL);
    if !path.exists() && backup(&path).is_file(){fs::rename(backup(&path),&path).map_err(error)?;}
    if !path.exists(){return Ok(Journal{version:1,..Default::default()});}
    if path.is_symlink(){return Err("Used-media journal cannot be a symbolic link".into());}
    let j:Journal=serde_json::from_slice(&fs::read(&path).map_err(error)?).map_err(|e|format!("Used-media journal needs recovery: {e}"))?;
    if j.version!=1{return Err("Unsupported used-media journal version".into());}
    for leftover in [backup(&path),pending_file(&path)] {
        if leftover.exists() {
            if leftover.is_symlink(){return Err("Unsafe used-media recovery state".into());}
            let old:Journal=serde_json::from_slice(&fs::read(&leftover).map_err(error)?).map_err(|_|"Changed journal recovery file preserved".to_owned())?;
            if old.version!=1{return Err("Unknown journal recovery state preserved".into());}
            fs::remove_file(leftover).map_err(error)?;
        }
    }
    Ok(j)
}
fn save(root:&Path,j:&Journal)->Result<(),String>{replace(&root.join(JOURNAL),&serde_json::to_vec_pretty(j).map_err(error)?)}
fn secure_parent(root:&Path,parent:&Path)->Result<(),String>{
    let relative=parent.strip_prefix(root).map_err(error)?;let mut at=root.to_path_buf();
    for part in relative.components(){if !matches!(part,Component::Normal(_)){return Err("Unsafe used-media destination".into());}at.push(part.as_os_str());
        match fs::symlink_metadata(&at){Ok(m) if m.is_dir()&&!m.file_type().is_symlink()=>{},Ok(_)=>return Err(format!("Unsafe used-media directory: {}",at.display())),Err(e) if e.kind()==std::io::ErrorKind::NotFound=>fs::create_dir(&at).map_err(error)?,Err(e)=>return Err(error(e))}
    }Ok(())
}
fn destination(root:&Path,project:&Path,source:&Path,reserved:&mut HashSet<PathBuf>)->Result<PathBuf,String>{
    let rel=source.strip_prefix(root).map_err(|_|"Used source is outside its imported library".to_owned())?;
    if rel.components().any(|p|!matches!(p,Component::Normal(_))) || rel.components().any(|p|p.as_os_str().to_string_lossy().eq_ignore_ascii_case("used")){return Err("Source is already in a used area".into());}
    let name=project.file_name().ok_or("Project must have a directory name")?;
    let base=root.join("used").join(name).join(rel);let stem=base.file_stem().unwrap_or_default().to_string_lossy();let ext=base.extension().map(|s|format!(".{}",s.to_string_lossy())).unwrap_or_default();
    for n in 1u32..100000 {let candidate=if n==1{base.clone()}else{base.with_file_name(format!("{stem} ({n}){ext}"))};
        if !candidate.exists() && !candidate.is_symlink() && reserved.insert(candidate.clone()){return Ok(candidate);}}
    Err("Too many filename collisions in used media".into())
}
fn rewrite(value:&mut serde_json::Value,map:&BTreeMap<String,String>){match value{serde_json::Value::String(s)=>{if let Some(next)=map.get(s){*s=next.clone();}},serde_json::Value::Array(v)=>for item in v {rewrite(item,map)},serde_json::Value::Object(v)=>for item in v.values_mut(){rewrite(item,map)},_=>{}}}
fn verify_document(bytes:&[u8])->Result<concat_project::Project,String>{
    let value=serde_json::from_slice(bytes).map_err(error)?;
    let project=concat_project::doc::from_document(&value).ok_or("Generated native project failed reopening")?;
    for item in &project.media {if !item.placeholder {File::open(&item.path).map_err(|e|format!("Project source cannot be read: {}: {e}",item.path))?;}}
    Ok(project)
}
fn validate_entry_paths(root:&Path,e:&Entry)->Result<(),String>{
    if !e.original.starts_with(root) || !e.relocated.starts_with(root.join("used")) || e.original.components().any(|c|matches!(c,Component::ParentDir)) || e.relocated.components().any(|c|matches!(c,Component::ParentDir)) {
        return Err("Unsafe used-media recovery path; files preserved".into());
    }
    let parent=e.original.parent().ok_or("No original parent")?;
    if parent.canonicalize().map_err(error)?!=parent {return Err("Unsafe original parent; files preserved".into());}
    if e.original.exists() && e.original.canonicalize().map_err(error)?!=e.original {return Err("Unsafe original path; files preserved".into());}
    secure_parent(root,e.relocated.parent().ok_or("No recovery parent")?)
}
fn move_file(source:&Path,target:&Path)->Result<(),String>{
    if fs::symlink_metadata(target).is_ok(){return Err(format!("Move destination already exists: {}",target.display()));}
    fs::rename(source,target).map_err(error)
}
fn rollback_links(root:&Path,p:&Pending)->Result<usize,String>{
    let cancel=AtomicBool::new(false);let mut removed=0;
    for entry in &p.entries {
        validate_entry_paths(root,entry)?;
        if entry.relocated.exists(){
            if !entry.relocated.is_file() || entry.relocated.is_symlink() || hash(&entry.relocated,&cancel)?!=entry.sha256 {return Err(format!("Changed recovery destination preserved: {}",entry.relocated.display()));}
            if p.moved {
                if entry.original.exists() || entry.original.is_symlink(){return Err("Original path occupied during rollback; both files preserved".into());}
                move_file(&entry.relocated,&entry.original)?;
            } else {
                if hash(&entry.original,&cancel)?!=entry.sha256 {return Err(format!("Original compatibility reference changed: {}",entry.original.display()));}
                fs::remove_file(&entry.relocated).map_err(error)?;
            }
            removed+=1;
        } else if p.moved && (stamp(&entry.original).ok()!=Some((entry.size,entry.modified_ns)) || hash(&entry.original,&cancel)?!=entry.sha256) {return Err("Original media changed or missing during rollback".into());}
    }Ok(removed)
}
fn recover_locked(root:&Path,j:&mut Journal)->Result<RecoveryReport,String>{
    let Some(p)=j.pending.clone() else{return Ok(Default::default());};
    if p.project.canonicalize().map_err(error)?!=p.project {return Err("Generated project location changed during recovery".into());}
    for e in &p.entries {validate_entry_paths(root,e)?;}
    let manifest=p.project.join("concat.json");
    if !manifest.exists() && backup(&manifest).exists(){fs::rename(backup(&manifest),&manifest).map_err(error)?;}
    let bytes=fs::read(&manifest).map_err(|e|format!("Cannot recover generated project {}: {e}",p.project.display()))?;
    let current=hash_bytes(&bytes);let mut report=RecoveryReport::default();
    if current==p.after_sha {
        verify_document(&bytes)?;let no_cancel=AtomicBool::new(false);
        for e in &p.entries {if hash(&e.relocated,&no_cancel)?!=e.sha256 || if p.moved {e.original.exists() || e.original.is_symlink()} else {hash(&e.original,&no_cancel)?!=e.sha256} {return Err("Used-media recovery hash mismatch; all files preserved".into());}}
        for e in p.entries {j.committed.insert(e.original.clone(),e);report.committed+=1;}
    } else if current==p.before_sha {report.rolled_back=rollback_links(root,&p)?;}
    else {return Err(format!("Generated project changed during recovery; no files overwritten: {}",p.project.display()));}
    // Temporary files are owned by this recorded operation; retain changed files for manual recovery.
    let temp=pending_file(&manifest);if temp.exists(){if hash_bytes(&fs::read(&temp).map_err(error)?)==p.after_sha {fs::remove_file(temp).map_err(error)?;}else{return Err("Changed pending project retained for recovery".into());}}
    let saved=backup(&manifest);if saved.exists(){if hash_bytes(&fs::read(&saved).map_err(error)?)==p.before_sha {fs::remove_file(saved).map_err(error)?;}else{return Err("Changed project backup retained for recovery".into());}}
    j.pending=None;save(root,j)?;Ok(report)
}
pub fn recover(library:&Path)->Result<RecoveryReport,String>{let root=library.canonicalize().map_err(error)?;let _lease=lock(&root)?;let mut j=journal(&root)?;recover_locked(&root,&mut j)}
/// Compatibility paths with unchanged source identity are hidden from the available-media inventory.
pub fn excluded_paths(library:&Path)->Result<HashSet<PathBuf>,String>{
    let root=library.canonicalize().map_err(error)?;let path=root.join(JOURNAL);
    if !path.exists() && !backup(&path).exists(){return Ok(HashSet::new());}
    // Normal inventory reads are read-only, including libraries on read-only volumes.
    let mut j=if path.is_file() && !path.is_symlink() && !backup(&path).exists() && !pending_file(&path).exists() {
        serde_json::from_slice::<Journal>(&fs::read(&path).map_err(error)?).map_err(error)?
    } else {let _lease=lock(&root)?;let mut j=journal(&root)?;recover_locked(&root,&mut j)?;j};
    if j.version!=1{return Err("Unsupported used-media inventory version".into());}
    if j.pending.is_some(){let _lease=lock(&root)?;j=journal(&root)?;recover_locked(&root,&mut j)?;}
    Ok(j.committed.iter().filter(|(path,e)|stamp(path).is_ok_and(|s|s==(e.size,e.modified_ns))).map(|(p,_)|p.clone()).collect())
}
pub fn relocate_used(library:&Path,project:&Path,consumed:&[PathBuf])->Result<RelocationReport,String>{relocate_with_cancel(library,project,consumed,&AtomicBool::new(false))}
pub fn relocate_with_cancel(library:&Path,project:&Path,consumed:&[PathBuf],cancel:&AtomicBool)->Result<RelocationReport,String>{relocate_inner(library,project,consumed,cancel,None)}
fn relocate_inner(library:&Path,project:&Path,consumed:&[PathBuf],cancel:&AtomicBool,fail_after:Option<usize>)->Result<RelocationReport,String>{
    let root=library.canonicalize().map_err(error)?;let project=project.canonicalize().map_err(error)?;
    let _lease=lock(&root)?;let mut j=journal(&root)?;recover_locked(&root,&mut j)?;check_cancel(cancel)?;
    let manifest=project.join("concat.json");if manifest.is_symlink(){return Err("Generated manifest cannot be a symlink".into());}
    let before=fs::read(&manifest).map_err(error)?;let native=verify_document(&before)?;
    let ids:HashSet<_>=native.timelines.iter().flat_map(|t|&t.clips).filter(|c|c.kind.is_visual()).map(|c|c.media_id.as_str()).collect();
    let actual:HashSet<PathBuf>=native.media.iter().filter(|m|ids.contains(m.id.as_str()) && matches!(m.kind,concat_project::model::MediaKind::Image|concat_project::model::MediaKind::Video)).map(|m|PathBuf::from(&m.path)).collect();
    let mut unique=std::collections::BTreeSet::new();for path in consumed {
        let absolute=if path.exists(){path.canonicalize().map_err(error)?}else{path.clone()};
        unique.insert(absolute);
    }
    if unique.is_empty(){return Err("No actually consumed picture media to archive".into());}
    let mut entries=Vec::new();let mut reserved=HashSet::new();let mut already_archived=0;
    for source in unique {
        check_cancel(cancel)?;
        if let Some(old)=j.committed.get(&source) {
            if old.project==project && actual.contains(&old.relocated) {
                if (source.exists() && hash(&source,cancel)?!=old.sha256) || hash(&old.relocated,cancel)?!=old.sha256 {return Err("Previously archived project media changed; files preserved".into());}
                already_archived+=1;continue;
            }
            if source.exists() && hash(&source,cancel)?==old.sha256 {return Err(format!("Source was already consumed; refresh the library: {}",source.display()));}
        }
        if !actual.contains(&source){return Err(format!("Refusing to archive unused media or narration: {}",source.display()));}
        let (size,modified_ns)=stamp(&source)?;let sha256=hash(&source,cancel)?;
        if stamp(&source)?!=(size,modified_ns){return Err("Media changed during archival preflight; refresh library".into());}
        let relocated=destination(&root,&project,&source,&mut reserved)?;
        secure_parent(&root,relocated.parent().ok_or("No destination parent")?)?;
        entries.push(Entry{original:source,relocated,size,modified_ns,sha256,project:project.clone()});
    }
    if entries.is_empty(){return Ok(RelocationReport{entries,compatibility_links:0,already_archived});}
    let map:BTreeMap<_,_>=entries.iter().map(|e|(e.original.to_string_lossy().into_owned(),e.relocated.to_string_lossy().into_owned())).collect();
    let mut document:serde_json::Value=serde_json::from_slice(&before).map_err(error)?;rewrite(&mut document,&map);
    let after=serde_json::to_vec_pretty(&document).map_err(error)?;
    let pending=Pending{project:project.clone(),before_sha:hash_bytes(&before),after_sha:hash_bytes(&after),entries:entries.clone(),moved:true};
    j.pending=Some(pending.clone());save(&root,&j)?;
    let outcome=(||->Result<(),String>{
        for (index,e) in entries.iter().enumerate(){check_cancel(cancel)?;
            if fail_after==Some(index){return Err("Injected archival failure".into());}
            secure_parent(&root,e.relocated.parent().ok_or("No destination parent")?)?;
            if stamp(&e.original)?!=(e.size,e.modified_ns) || hash(&e.original,cancel)?!=e.sha256 {return Err("Media changed before move".into());}
            move_file(&e.original,&e.relocated)?;
            if stamp(&e.relocated)?!=(e.size,e.modified_ns) || hash(&e.relocated,cancel)?!=e.sha256 {return Err("Media changed during archival; transaction rolled back".into());}
        }
        check_cancel(cancel)?;if hash_bytes(&fs::read(&manifest).map_err(error)?)!=pending.before_sha{return Err("Generated project was edited during archival".into());}
        let reopened=verify_document(&after)?;
        if reopened.active().clips.len()!=native.active().clips.len(){return Err("Archival changed the editable clip inventory".into());}
        replace(&manifest,&after)?;verify_document(&fs::read(&manifest).map_err(error)?)?;Ok(())
    })();
    if let Err(reason)=outcome {
        let now=fs::read(&manifest).map_err(error)?;
        if hash_bytes(&now)==pending.after_sha {replace(&manifest,&before)?;}
        match recover_locked(&root,&mut j){Ok(_)=>return Err(format!("{reason}. Original media and valid project retained at {}",project.display())),Err(recovery)=>return Err(format!("{reason}. Recovery required: {recovery}"))}
    }
    for e in &entries {j.committed.insert(e.original.clone(),e.clone());}j.pending=None;
    // If this final journal write is interrupted, recovery recognizes the committed manifest hash.
    save(&root,&j).map_err(|e|format!("Project sources are safe at {}; journal recovery required: {e}",project.display()))?;
    Ok(RelocationReport{compatibility_links:0,entries,already_archived})
}

#[cfg(test)]
mod tests {
    use super::*;
    use concat_project::doc::{to_document,DocumentSettings};
    use concat_project::model::{Clip,ClipKind,MediaItem,MediaKind,Project};
    use std::sync::Arc;
    struct Fixture {root:PathBuf,project:PathBuf,paths:Vec<PathBuf>,audio:PathBuf}
    impl Drop for Fixture {fn drop(&mut self){let _=fs::remove_dir_all(&self.root);}}
    fn fixture(count:usize)->Fixture {
        let root=std::env::temp_dir().join(format!("concat-used-{}-{}",std::process::id(),std::time::SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()));
        fs::create_dir_all(root.join("Match/Éala")).unwrap();let root=root.canonicalize().unwrap();
        let project=root.join("generated");fs::create_dir(&project).unwrap();
        let audio=root.join("narration.wav");fs::write(&audio,b"unchanged narration").unwrap();
        let mut native=Project::new();let mut paths=Vec::new();
        for index in 0..count {
            let path=root.join(format!("Match/Éala/shot {index}.{}",if index%3==0{"mp4"}else{"jpg"}));fs::write(&path,format!("unique source bytes {index}")).unwrap();
            let id=format!("m{index}");let video=index%3==0;
            native.media.push(MediaItem{id:id.clone(),path:path.to_string_lossy().into_owned(),kind:if video{MediaKind::Video}else{MediaKind::Image},width:Some(1920),height:Some(1080),duration:Some(20.0),..Default::default()});
            let mut clip=Clip::blank(format!("c{index}"),"T1",if video{ClipKind::Video}else{ClipKind::Image},"shot",index as f64,1.0);clip.media_id=id;native.active_mut().clips.push(Arc::new(clip));paths.push(path);
        }
        native.media.push(MediaItem{id:"voice".into(),path:audio.to_string_lossy().into_owned(),kind:MediaKind::Audio,duration:Some(20.0),..Default::default()});
        let mut voice=Clip::blank("voice","T3",ClipKind::Audio,"narration",0.0,20.0);voice.media_id="voice".into();native.active_mut().clips.push(Arc::new(voice));
        let settings=DocumentSettings{name:"Story".into(),width:1920,height:1080,rate_num:30,rate_den:1};
        fs::write(project.join("concat.json"),serde_json::to_vec_pretty(&to_document(&settings,&native)).unwrap()).unwrap();
        Fixture{root,project,paths,audio}
    }
    #[test]
    fn thirteen_unique_consumed_files_move_and_generated_project_reopens(){
        let f=fixture(13);let old=fs::read(f.project.join("concat.json")).unwrap();
        let unused=f.root.join("unused.jpg");fs::write(&unused,b"unused").unwrap();
        let mut consumed=f.paths.clone();consumed.extend(f.paths.clone());
        let report=relocate_used(&f.root,&f.project,&consumed).unwrap();assert_eq!(report.entries.len(),13);assert_eq!(report.compatibility_links,0);
        let new=verify_document(&fs::read(f.project.join("concat.json")).unwrap()).unwrap();
        let old=concat_project::doc::from_document(&serde_json::from_slice(&old).unwrap()).unwrap();assert_eq!(new.active().clips,old.active().clips);
        for e in &report.entries {assert!(e.relocated.starts_with(f.root.join("used/generated/Match/Éala")));assert!(!e.original.exists());assert!(e.relocated.is_file());}
        assert!(new.media.iter().filter(|m|m.kind!=MediaKind::Audio).all(|m|Path::new(&m.path).starts_with(f.root.join("used"))));
        assert_eq!(new.media.iter().find(|m|m.kind==MediaKind::Audio).unwrap().path,f.audio.to_string_lossy());
        let excluded=excluded_paths(&f.root).unwrap();assert_eq!(excluded.len(),0);assert!(!excluded.contains(&unused));assert!(!excluded.contains(&f.audio));
        let repeated=relocate_used(&f.root,&f.project,&f.paths).unwrap();assert!(repeated.entries.is_empty());
    }
    #[test]
    fn failed_link_sequence_rolls_back_and_preserves_project_and_originals(){
        let f=fixture(3);let before=fs::read(f.project.join("concat.json")).unwrap();
        let result=relocate_inner(&f.root,&f.project,&f.paths,&AtomicBool::new(false),Some(1));assert!(result.is_err());
        assert_eq!(fs::read(f.project.join("concat.json")).unwrap(),before);
        for p in &f.paths {assert!(p.is_file());}
        let j=journal(&f.root).unwrap();assert!(j.pending.is_none());assert!(j.committed.is_empty());
        assert_eq!(excluded_paths(&f.root).unwrap().len(),0);
    }
    #[test]
    fn collisions_never_overwrite_and_cancel_never_archives(){
        let f=fixture(1);let dest=f.root.join("used/generated/Match/Éala/shot 0.mp4");fs::create_dir_all(dest.parent().unwrap()).unwrap();fs::write(&dest,b"existing user file").unwrap();
        assert!(relocate_with_cancel(&f.root,&f.project,&f.paths,&AtomicBool::new(true)).is_err());assert!(excluded_paths(&f.root).unwrap().is_empty());
        let report=relocate_used(&f.root,&f.project,&f.paths).unwrap();assert!(report.entries[0].relocated.to_string_lossy().contains("(2)"));assert_eq!(fs::read(&dest).unwrap(),b"existing user file");
    }
    #[test]
    fn narration_and_unconsumed_sources_are_never_retired(){
        let f=fixture(1);let unreferenced=f.root.join("other.jpg");fs::write(&unreferenced,b"other").unwrap();
        for path in [&f.audio,&unreferenced] {assert!(relocate_used(&f.root,&f.project,&[path.clone()]).is_err());assert!(path.is_file());}
        assert!(excluded_paths(&f.root).unwrap().is_empty());
    }
    #[test]
    fn interrupted_committed_manifest_recovers_forward_without_duplicate_files(){
        let f=fixture(1);let manifest=f.project.join("concat.json");let before=fs::read(&manifest).unwrap();
        let destination=f.root.join("used/generated/Match/Éala/shot 0.mp4");secure_parent(&f.root,destination.parent().unwrap()).unwrap();move_file(&f.paths[0],&destination).unwrap();
        let (size,modified_ns)=stamp(&destination).unwrap();let entry=Entry{original:f.paths[0].clone(),relocated:destination.clone(),size,modified_ns,sha256:hash(&destination,&AtomicBool::new(false)).unwrap(),project:f.project.clone()};
        let mut value:serde_json::Value=serde_json::from_slice(&before).unwrap();rewrite(&mut value,&BTreeMap::from([(entry.original.to_string_lossy().into_owned(),entry.relocated.to_string_lossy().into_owned())]));let after=serde_json::to_vec_pretty(&value).unwrap();
        let j=Journal{version:1,pending:Some(Pending{project:f.project.clone(),before_sha:hash_bytes(&before),after_sha:hash_bytes(&after),entries:vec![entry],moved:true}),..Default::default()};save(&f.root,&j).unwrap();replace(&manifest,&after).unwrap();
        assert_eq!(recover(&f.root).unwrap().committed,1);assert_eq!(excluded_paths(&f.root).unwrap().len(),0);assert!(destination.is_file());assert!(!f.paths[0].exists());
    }
    #[test]
    fn interrupted_precommit_links_recover_back_and_readers_keep_valid_paths(){
        let f=fixture(1);let before=fs::read(f.project.join("concat.json")).unwrap();let target=f.root.join("used/recover.mp4");secure_parent(&f.root,target.parent().unwrap()).unwrap();move_file(&f.paths[0],&target).unwrap();let(size,modified_ns)=stamp(&target).unwrap();
        let e=Entry{original:f.paths[0].clone(),relocated:target.clone(),size,modified_ns,sha256:hash(&target,&AtomicBool::new(false)).unwrap(),project:f.project.clone()};
        save(&f.root,&Journal{version:1,pending:Some(Pending{project:f.project.clone(),before_sha:hash_bytes(&before),after_sha:"not committed".into(),entries:vec![e],moved:true}),..Default::default()}).unwrap();
        assert_eq!(recover(&f.root).unwrap().rolled_back,1);assert!(!target.exists());assert!(f.paths[0].exists());verify_document(&before).unwrap();
    }
    #[test]
    fn completed_archive_reports_remaining_inventory_refresh_failures(){
        let f=fixture(1);
        // This new remaining candidate has no cache and the source-check probe is unavailable.
        // An archive can commit successfully while this independent inventory refresh fails.
        let remaining=f.root.join("unreadable.jpg");fs::write(&remaining,b"not a decodable picture").unwrap();
        let cache=f.root.join("catalog.json");
        let finish=finish(&f.root,&f.project,&f.paths,&cache,&AtomicBool::new(false));
        assert_eq!(finish.result.unwrap().entries.len(),1);
        let refreshed=finish.refresh.unwrap();assert_eq!(refreshed.failures.len(),1);
        assert!(finish.cache_warning.unwrap().contains("1 remaining media could not be refreshed"));
        assert!(remaining.is_file());
        assert_eq!(excluded_paths(&f.root).unwrap().len(),0);
        verify_document(&fs::read(f.project.join("concat.json")).unwrap()).unwrap();
    }
    #[test]
    fn library_lock_prevents_two_windows_consuming_the_same_sources(){
        let f=fixture(1);let _lease=lock(&f.root).unwrap();assert!(relocate_used(&f.root,&f.project,&f.paths).unwrap_err().contains("Another C Builder"));
    }
    #[cfg(unix)]
    #[test]
    fn used_directory_symlink_is_refused_without_touching_its_target(){
        let f=fixture(1);let other=f.root.join("outside");fs::create_dir(&other).unwrap();std::os::unix::fs::symlink(&other,f.root.join("used")).unwrap();
        assert!(relocate_used(&f.root,&f.project,&f.paths).is_err());assert_eq!(fs::read_dir(&other).unwrap().count(),0);assert!(f.paths[0].is_file());
    }
}

#[derive(Debug)]
pub struct Finish {
    pub result:Result<RelocationReport,String>,
    pub refresh:Option<crate::c_builder::ScanReport>,
    pub cache_warning:Option<String>,
}
/// Complete relocation, cache updates and visible inventory before the UI reports completion.
pub fn finish(library:&Path,project:&Path,consumed:&[PathBuf],cache:&Path,cancel:&AtomicBool)->Finish {
    let result=relocate_with_cancel(library,project,consumed,cancel);
    let mut refresh=None;let mut warnings=Vec::new();
    if result.is_ok(){
        let names:Vec<_>=consumed.iter().map(|p|p.to_string_lossy().into_owned()).collect();
        if let Err(e)=crate::c_builder::mark_used(cache,&names){warnings.push(e);}
        match crate::c_builder::scan(library,cache,|_,_|{}) {
            Ok(report)=>{
                if !report.failures.is_empty(){warnings.push(format!("{} remaining media could not be refreshed: {}",report.failures.len(),report.failures.join("; ")));}
                refresh=Some(report);
            },
            Err(e)=>warnings.push(e),
        }
    }
    Finish {result,refresh,cache_warning:(!warnings.is_empty()).then(||warnings.join("; "))}
}
