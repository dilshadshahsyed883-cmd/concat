// SPDX-License-Identifier: AGPL-3.0-or-later
//! C Builder: persistent, non-destructive media catalogue and native Concat timeline generation.
//! All expensive probes happen on the caller's background worker, never during timeline generation.
#![allow(missing_docs, dead_code)]
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use serde::{Deserialize, Serialize};
use concat_host::media;
use concat_project::doc::{self, DocumentSettings};
use concat_project::model::{Clip, ClipKind, MediaItem, MediaKind, Project};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Asset {
    pub path: String,
    pub keyword: String,
    pub kind: String,
    pub size: u64,
    pub modified_ns: u64,
    pub duration: Option<f64>,
    pub width: Option<u32>,
    pub height: Option<u32>,
    pub frame_rate: Option<f64>,
    pub frame_rate_fraction: Option<String>,
    pub video_codec: Option<String>,
    pub audio_codec: Option<String>,
    pub has_audio: bool,
    #[serde(default)]
    pub used_count: u32,
}
#[derive(Default, Serialize, Deserialize)]
struct Cache { #[serde(default)] entries: BTreeMap<String, Asset> }
#[derive(Clone, Debug)]
pub struct ScanReport {
    pub root: PathBuf,
    pub assets: Vec<Asset>,
    pub probed: usize,
    pub reused: usize,
    pub removed: usize,
    pub failures: Vec<String>,
}
impl ScanReport {
    pub fn images(&self) -> usize { self.assets.iter().filter(|a| a.kind == "image").count() }
    pub fn videos(&self) -> usize { self.assets.iter().filter(|a| a.kind == "video").count() }
    pub fn keywords(&self) -> String {
        let mut totals: BTreeMap<String, (usize, usize)> = BTreeMap::new();
        for a in &self.assets {
            let entry = totals.entry(a.keyword.clone()).or_default();
            if a.kind == "image" { entry.0 += 1; } else { entry.1 += 1; }
        }
        totals.into_iter().map(|(k, (i, v))| format!("{k}: {i} images, {v} videos"))
            .collect::<Vec<_>>().join("\n")
    }
}
fn keyword(path: &Path) -> String {
    let stem = path.file_stem().unwrap_or_default().to_string_lossy();
    let trimmed = stem.trim_end_matches(|c: char| c.is_ascii_digit());
    let prefix = trimmed.trim_end_matches(|c: char| c == ' ' || c == '_' || c == '-');
    if prefix.len() < stem.len() && !prefix.is_empty() { prefix.into() } else { stem.into_owned() }
}
fn supported(path: &Path) -> bool {
    matches!(path.extension().unwrap_or_default().to_string_lossy().to_ascii_lowercase().as_str(),
        "jpg"|"jpeg"|"png"|"webp"|"bmp"|"gif"|"tif"|"tiff"|"heic"|"heif"|"avif"|
        "mp4"|"m4v"|"mov"|"mkv"|"webm"|"avi")
}
fn stamp(metadata: &fs::Metadata) -> u64 {
    metadata.modified().ok().and_then(|m| m.duration_since(UNIX_EPOCH).ok())
        .and_then(|d| u64::try_from(d.as_nanos()).ok()).unwrap_or(0)
}
fn read_cache(path: &Path) -> Result<Cache, String> {
    if !path.exists() { return Ok(Cache::default()); }
    serde_json::from_slice(&fs::read(path).map_err(|e| e.to_string())?)
        .map_err(|e| format!("Media cache unreadable; existing cache preserved: {e}"))
}
fn save_cache(path: &Path, cache: &Cache) -> Result<(), String> {
    if let Some(parent) = path.parent() { fs::create_dir_all(parent).map_err(|e| e.to_string())?; }
    let tmp = path.with_extension("pending");
    let backup = path.with_extension("backup");
    let bytes = serde_json::to_vec_pretty(cache).map_err(|e| e.to_string())?;
    fs::write(&tmp, bytes).map_err(|e| e.to_string())?;
    if backup.exists() { fs::remove_file(&backup).map_err(|e| e.to_string())?; }
    let existing = path.exists();
    if existing { fs::rename(&path, &backup).map_err(|e| e.to_string())?; }
    if let Err(e) = fs::rename(&tmp, path) {
        if existing { let _ = fs::rename(&backup, &path); }
        return Err(e.to_string());
    }
    if existing { let _ = fs::remove_file(backup); }
    Ok(())
}
fn collect(folder: &Path) -> Result<Vec<PathBuf>, String> {
    if !folder.is_dir() { return Err("Choose an existing media folder".into()); }
    let mut todo = vec![folder.to_path_buf()];
    let mut files = Vec::new();
    while let Some(folder) = todo.pop() {
        let children = fs::read_dir(&folder).map_err(|e| format!("{}: {e}", folder.display()))?;
        for entry in children {
            let entry = entry.map_err(|e| e.to_string())?;
            let ty = entry.file_type().map_err(|e| e.to_string())?;
            if ty.is_symlink() { continue; }
            if ty.is_dir() {
                if !entry.file_name().to_string_lossy().eq_ignore_ascii_case("used") {
                    todo.push(entry.path());
                }
            } else if ty.is_file() && supported(&entry.path()) {
                files.push(entry.path());
            }
        }
    }
    files.sort();
    Ok(files)
}
pub fn scan(folder: &Path, cache_path: &Path, mut progress: impl FnMut(usize, usize))
    -> Result<ScanReport, String> {
    let root = folder.canonicalize().map_err(|e| e.to_string())?;
    let excluded=crate::c_builder_used::excluded_paths(&root)?;
    let files:Vec<_>=collect(&root)?.into_iter().filter(|p|!excluded.contains(p)).collect();
    let mut cache = read_cache(cache_path)?;
    let mut assets = Vec::with_capacity(files.len());
    let mut seen = HashSet::new();
    let mut probed = 0; let mut reused = 0; let mut failures = Vec::new();
    progress(0, files.len());
    for (index, path) in files.iter().enumerate() {
        let absolute = path.canonicalize().map_err(|e| e.to_string())?;
        let name = absolute.to_string_lossy().into_owned();
        seen.insert(name.clone());
        let metadata = fs::metadata(&absolute).map_err(|e| e.to_string())?;
        let old = cache.entries.get(&name);
        let hit = old.filter(|a| a.size == metadata.len() && a.modified_ns == stamp(&metadata));
        let asset = if let Some(old) = hit {
            reused += 1; Some(old.clone())
        } else {
            match media::probe(&name) {
                Ok(summary) if matches!(summary.kind, MediaKind::Video | MediaKind::Image) => {
                    probed += 1;
                    let a = Asset {
                        path: name.clone(), keyword: keyword(&absolute),
                        kind: if summary.kind == MediaKind::Image { "image" } else { "video" }.into(),
                        size: metadata.len(), modified_ns: stamp(&metadata), duration: summary.duration,
                        width: summary.video.as_ref().map(|s| s.width),
                        height: summary.video.as_ref().map(|s| s.height),
                        frame_rate: summary.video.as_ref().map(|s| s.frame_rate),
                        frame_rate_fraction: summary.video.as_ref().map(|s| s.frame_rate_fraction.clone()),
                        video_codec: summary.video.as_ref().map(|s| s.codec.clone()),
                        audio_codec: summary.audio.as_ref().map(|s| s.codec.clone()),
                        has_audio: summary.audio.is_some(),
                        used_count: old.map(|a| a.used_count).unwrap_or_default(),
                    };
                    cache.entries.insert(name, a.clone());
                    Some(a)
                }
                Ok(_) => { failures.push(format!("Not a supported visual: {}", absolute.display())); None }
                Err(e) => { failures.push(format!("{}: {e}", absolute.display())); None }
            }
        };
        if let Some(asset) = asset { assets.push(asset); }
        progress(index + 1, files.len());
    }
    let before = cache.entries.len();
    cache.entries.retain(|p, _| !Path::new(p).starts_with(&root) || seen.contains(p));
    let removed = before - cache.entries.len();
    save_cache(cache_path, &cache)?;
    Ok(ScanReport { root, assets, probed, reused, removed, failures })
}
pub fn mark_used(cache_path: &Path, paths: &[String]) -> Result<(), String> {
    let mut cache = read_cache(cache_path)?;
    for path in paths {
        if let Some(asset) = cache.entries.get_mut(path) {
            asset.used_count = asset.used_count.saturating_add(1);
        }
    }
    save_cache(cache_path, &cache)
}
#[derive(Clone, Debug)]
pub struct Shot {
    pub asset: Asset,
    pub start_us: u64,
    pub duration_us: u64,
    pub trim_us: u64,
}
impl Shot { pub fn end_us(&self) -> u64 { self.start_us + self.duration_us } }
fn strengths(input: &str, pool: &[&Asset]) -> Result<HashMap<String, u32>, String> {
    let mut known: HashMap<String, String> = HashMap::new();
    for a in pool { known.insert(a.keyword.to_lowercase(), a.keyword.clone()); }
    if known.is_empty() { return Err("No available media for this media type".into()); }
    if input.trim().is_empty() {
        return Ok(known.into_values().map(|name| (name, 1)).collect());
    }
    let mut requested = HashMap::new();
    for pair in input.split([',', ';', '\n']) {
        if pair.trim().is_empty() { continue; }
        let (name, value) = pair.split_once('=')
            .ok_or_else(|| format!("Use Keyword=Strength, e.g. Alex Eala=60: {pair}"))?;
        let normalized = known.get(&name.trim().to_lowercase())
            .ok_or_else(|| format!("Unknown keyword: {}", name.trim()))?;
        let weight = value.trim().parse::<u32>().map_err(|_| format!("Invalid strength: {pair}"))?;
        if weight > 100 { return Err(format!("Strength must be between 0 and 100: {pair}")); }
        if weight > 0 { requested.insert(normalized.clone(), weight); }
    }
    if requested.is_empty() { return Err("Select at least one keyword".into()); }
    Ok(requested)
}
pub fn plan(assets: &[Asset], seconds: f64, image_weights: &str, video_weights: &str,
    coverage: u32, seed: u64) -> Result<Vec<Shot>, String> {
    if coverage > 100 { return Err("Coverage must be 0–100%".into()); }
    let mut profile = concat_project::planning::Profile::default();
    profile.curve = vec![concat_project::planning::Anchor { at: 0.0, video: coverage as f64/100.0 },
        concat_project::planning::Anchor { at: 1.0, video: coverage as f64/100.0 }];
    profile.seed = seed;
    profile.max_videos = 0;
    plan_profile(assets, seconds, image_weights, video_weights, &profile).map(|p| p.0)
}
pub fn plan_profile(assets: &[Asset], seconds: f64, image_weights: &str, video_weights: &str,
    profile: &concat_project::planning::Profile) -> Result<(Vec<Shot>, String), String> {
    if !seconds.is_finite() || seconds <= 0.0 || seconds > 86_400.0 {
        return Err("Narration duration must be between 0 and 24 hours".into());
    }
    let images: Vec<_> = assets.iter().filter(|a| a.kind == "image").collect();
    let videos: Vec<_> = assets.iter().filter(|a| a.kind == "video").collect();
    let (iw,vw)=if let Some(selection)=&profile.media_selection {
        let weights=|map:&BTreeMap<String,concat_project::planning::KeywordSelection>|map.iter().filter(|(_,v)|v.selected&&v.weight>0).map(|(k,v)|(k.clone(),v.weight)).collect::<HashMap<_,_>>();
        (weights(&selection.images),if selection.videos_enabled {weights(&selection.videos)}else{HashMap::new()})
    } else {
        (if images.is_empty(){HashMap::new()}else{strengths(image_weights,&images)?},if videos.is_empty(){HashMap::new()}else{strengths(video_weights,&videos)?})
    };
    let mut group_totals=HashMap::<(String,String),f64>::new();
    for asset in assets { *group_totals.entry((asset.kind.clone(),asset.keyword.clone())).or_default()+=1.0/(1.0+f64::from(asset.used_count)); }
    let sources: Vec<_> = assets.iter().map(|a| concat_project::planning::Source {
        id: a.path.clone(), group:a.keyword.clone(), video: a.kind == "video",
        duration_us: a.duration.filter(|d| d.is_finite() && *d > 0.0)
            .map(|d| (d*1e6) as u64).unwrap_or(0),
        weight: f64::from(*if a.kind == "video" { vw.get(&a.keyword) } else { iw.get(&a.keyword) }.unwrap_or(&0))
            / (1.0 + f64::from(a.used_count)) / group_totals[&(a.kind.clone(),a.keyword.clone())],
    }).collect();
    if !sources.iter().any(|s|s.weight>0.) {return Err("Select at least one image keyword with a positive slider weight, or enable and select videos".into());}
    if profile.pins.iter().any(|p|sources.iter().find(|s|s.id==p.source_id).is_none_or(|s|s.weight<=0.)){return Err("A pinned shot belongs to an excluded keyword; clear that pin or enable its media group".into());}
    let result = concat_project::planning::generate(&sources, (seconds*1e6).round() as u64, profile)?;
    let stats = result.stats(&sources);
    let count = stats.images+stats.videos;
    let mut summary = format!("{} shots; video {:.1}% by count / {:.1}% by seconds. Seed {}.",
        count, stats.videos as f64/count as f64*100.0, stats.video_us as f64/(seconds*1e6)*100.0, profile.seed);
    if let Some(target)=profile.overall_video_percent {summary.push_str(&format!("\nOverall video duration target: {target}%"));}
    for pair in profile.curve.windows(2) {
        let lo=(pair[0].at*seconds*1e6).round() as u64;let hi=(pair[1].at*seconds*1e6).round() as u64;
        let(mut video_us,mut image_us,mut videos,mut images)=(0_u64,0_u64,0_u64,0_u64);
        for shot in &result.shots {let overlap=(shot.start_us+shot.duration_us).min(hi).saturating_sub(shot.start_us.max(lo));
            if sources[shot.source].video {video_us+=overlap;if shot.start_us>=lo&&shot.start_us<hi{videos+=1;}}
            else{image_us+=overlap;if shot.start_us>=lo&&shot.start_us<hi{images+=1;}}
        }
        summary.push_str(&format!("\nZone {:.0}–{:.0}%: {videos} videos / {images} images; video {:.1}% by count, {:.1}% by seconds",pair[0].at*100.,pair[1].at*100.,
            if videos+images>0{videos as f64/(videos+images) as f64*100.}else{0.},if video_us+image_us>0{video_us as f64/(video_us+image_us) as f64*100.}else{0.}));
    }
    for warning in result.warnings { summary.push_str(&format!("\n{warning}")); }
    let shots: Vec<_> = result.shots.into_iter().map(|s| Shot { asset: assets[s.source].clone(),
        start_us: s.start_us, duration_us: s.duration_us, trim_us: s.trim_us }).collect();
    validate(&shots, (seconds*1e6).round() as u64)?;
    for (i,s) in shots.iter().enumerate().take(200) {
        summary.push_str(&format!("\n{}: {:.2}–{:.2}s {} {}", i+1, s.start_us as f64/1e6,
            s.end_us() as f64/1e6, s.asset.kind, Path::new(&s.asset.path).file_name().unwrap_or_default().to_string_lossy()));
    }
    if shots.len() > 200 { summary.push_str("\nPreview limited to first 200 shots."); }
    Ok((shots, summary))
}
pub fn validate(shots: &[Shot], total: u64) -> Result<(), String> {
    if shots.is_empty() { return Err("Timeline has no visual media".into()); }
    let mut cursor = 0;
    for shot in shots {
        if shot.start_us != cursor || shot.duration_us == 0 { return Err("Timeline contains a gap or overlap".into()); }
        if shot.asset.kind == "video" && shot.asset.duration.unwrap_or(0.0)*1e6+1.0
            < shot.trim_us.saturating_add(shot.duration_us) as f64 { return Err("Video trim exceeds source".into()); }
        if !matches!(shot.asset.kind.as_str(), "image" | "video") { return Err("Unsupported visual type".into()); }
        cursor = cursor.checked_add(shot.duration_us).ok_or("Timeline overflow")?;
    }
    if cursor != total { return Err("Timeline endpoint differs from narration".into()); }
    Ok(())
}
pub fn narration_seconds(path: &Path) -> Result<f64, String> {
    let summary = media::probe(&path.to_string_lossy())?;
    if summary.kind != MediaKind::Audio { return Err("Please select an audio narration".into()); }
    let seconds = summary.duration.ok_or("Narration has no audio duration")?;
    if seconds <= 0.0 || !seconds.is_finite() { return Err("Invalid narration duration".into()); }
    Ok(seconds)
}
fn media_item(asset: &Asset, id: String) -> MediaItem {
    MediaItem {
        id, path: asset.path.clone(),
        name: Path::new(&asset.path).file_name().unwrap_or_default().to_string_lossy().into_owned(),
        duration: asset.duration,
        kind: if asset.kind == "image" { MediaKind::Image } else { MediaKind::Video },
        width: asset.width, height: asset.height, frame_rate: asset.frame_rate,
        frame_rate_fraction: asset.frame_rate_fraction.clone(),
        video_codec: asset.video_codec.clone(), audio_codec: asset.audio_codec.clone(),
        has_audio: asset.has_audio, ..MediaItem::default()
    }
}
pub fn write_native(shots: &[Shot], narration: &Path, duration: f64,
    parent: &Path, name: &str) -> Result<PathBuf, String> {
    write_native_with_profile(shots,narration,duration,parent,name,None)
}
pub fn write_native_with_profile(shots:&[Shot],narration:&Path,duration:f64,parent:&Path,name:&str,
    profile:Option<&concat_project::planning::Profile>)->Result<PathBuf,String>{
    if let Some(profile)=profile {profile.validate()?;}

    let base_name=concat_project::naming::basename(name);
    let total = (duration * 1_000_000.0).round() as u64;
    validate(shots, total)?;
    // Claim a unique directory atomically; neither old projects nor arbitrary folders are overwritten.
    let mut chosen=None;
    for n in 1..10000 {
        let name=concat_project::naming::numbered(&base_name,n);
        let root=parent.join(&name);
        match fs::create_dir(&root) {
            Ok(())=>{chosen=Some((root,name));break;}
            Err(e) if e.kind()==std::io::ErrorKind::AlreadyExists=>continue,
            Err(e)=>return Err(format!("Could not create project directory: {e}")),
        }
    }
    let (root,name)=chosen.ok_or("Too many project name collisions")?;
    let outcome = (|| -> Result<(), String> {
        let mut project = Project::new();
        if let Some(profile)=profile {project.extra.insert("cBuilderPlanning".into(),serde_json::to_value(profile).map_err(|e|e.to_string())?);}
        let mut media_ids: HashMap<String, String> = HashMap::new();
        for shot in shots {
            let source = Path::new(&shot.asset.path);
            let metadata = fs::metadata(source)
                .map_err(|_| format!("Missing source: {}", shot.asset.path))?;
            if metadata.len() != shot.asset.size || stamp(&metadata) != shot.asset.modified_ns {
                return Err(format!("Media changed since scanning; refresh library: {}", shot.asset.path));
            }
            if !media_ids.contains_key(&shot.asset.path) {
                let id = format!("m{}", project.media.len() + 1);
                project.media.push(media_item(&shot.asset, id.clone()));
                media_ids.insert(shot.asset.path.clone(), id);
            }
        }
        let audio_path = narration.canonicalize().map_err(|e| e.to_string())?;
        let audio_name = audio_path.to_string_lossy().into_owned();
        let audio_id = format!("m{}", project.media.len() + 1);
        project.media.push(MediaItem {
            id: audio_id.clone(), path: audio_name.clone(),
            name: audio_path.file_name().unwrap_or_default().to_string_lossy().into_owned(),
            duration: Some(duration), kind: MediaKind::Audio,
            audio_codec: None,
            has_audio: true, ..MediaItem::default()
        });
        let timeline = project.active_mut();
        timeline.name = name.to_owned();
        for (i, shot) in shots.iter().enumerate() {
            let is_image = shot.asset.kind == "image";
            // The visual track is intentionally shared: the planned image/video
            // sequence is one contiguous picture lane. Narration stays on T3.
            let mut clip = Clip::blank(format!("c{}", i + 1),
                "T1",
                if is_image { ClipKind::Image } else { ClipKind::Video },
                Path::new(&shot.asset.path).file_name().unwrap_or_default().to_string_lossy(),
                shot.start_us as f64 / 1e6, shot.duration_us as f64 / 1e6);
            clip.media_id = media_ids[&shot.asset.path].clone();
            clip.source_start = shot.trim_us as f64 / 1e6;
            if !is_image { clip.muted = Some(true); clip.volume = 0.0; }
            timeline.clips.push(Arc::new(clip));
        }
        let mut voice = Clip::blank(format!("c{}", shots.len() + 1), "T3",
            ClipKind::Audio, audio_path.file_name().unwrap_or_default().to_string_lossy(),
            0.0, duration);
        voice.media_id = audio_id;
        timeline.clips.push(Arc::new(voice));
        let settings = DocumentSettings {
            name: name.to_owned(), width: 1920, height: 1080, rate_num: 30, rate_den: 1
        };
        let document = doc::to_document(&settings, &project);
        if doc::from_document(&document).is_none() {
            return Err("Generated project failed Concat round-trip verification".into());
        }
        fs::write(root.join("concat.json"),
            serde_json::to_vec_pretty(&document).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
        Ok(())
    })();
    if outcome.is_err() { let _ = fs::remove_dir_all(&root); }
    outcome.map(|()| root)
}
pub fn random_seed() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(1)
}
#[cfg(test)]
mod tests {
    use super::*;
    fn image(name: &str) -> Asset {
        Asset { path: format!("/fake/{name}.jpg"), keyword: "Eala".into(), kind: "image".into(),
            size: 10, modified_ns: 1, duration: None, width: Some(1920), height: Some(1080),
            frame_rate: None, frame_rate_fraction: None, video_codec: None, audio_codec: None,
            has_audio: false, used_count: 0 }
    }
    #[test]
    fn successful_build_reset_keeps_preferences_but_excludes_every_keyword() {
        use concat_project::planning::{KeywordSelection, Profile};
        let mut profile = Profile::default();
        profile.seed = 42;
        profile.overall_video_percent = Some(30);
        let selection = profile.media_selection.get_or_insert_with(Default::default);
        selection.videos_enabled = true;
        selection.images.insert("Eala".into(), KeywordSelection { selected: true, weight: 40 });
        selection.images.insert("Osaka".into(), KeywordSelection { selected: true, weight: 60 });
        selection.videos.insert("Eala".into(), KeywordSelection { selected: true, weight: 100 });
        selection.videos.insert("Other".into(), KeywordSelection { selected: false, weight: 0 });
        let saved_project_profile = profile.clone();
        let mut state = State { profile_json: serde_json::to_string(&profile).unwrap(), ..Default::default() };
        state.reset_keyword_selections_after_build().unwrap();
        assert_eq!(state.remembered_keyword_weight(false, "Eala"), 40);
        assert_eq!(state.remembered_keyword_weight(false, "Osaka"), 60);
        assert_eq!(state.remembered_keyword_weight(true, "Eala"), 100);
        assert_eq!(state.remembered_keyword_weight(true, "Other"), 0);
        let draft = Profile::from_json(&state.profile_json).unwrap();
        let mut expected = saved_project_profile.clone();
        let empty = expected.media_selection.as_mut().unwrap();
        empty.images.clear();
        empty.videos.clear();
        assert_eq!(draft, expected);
        let error = plan_profile(&[image("one")], 10., "", "", &draft).err().unwrap();
        assert!(error.contains("Select at least one"));
        state.reset_keyword_selections_after_build().unwrap();
        assert_eq!(state.remembered_keyword_weight(false, "Eala"), 40);
        state.forget_keyword_weight(false, "Eala");
        assert_eq!(state.remembered_keyword_weight(false, "Eala"), 0);
        assert_eq!(state.remembered_keyword_weight(true, "Eala"), 100);
        assert!(saved_project_profile.media_selection.unwrap().images["Eala"].selected);
    }
    #[test]
    fn keyword_suffixes_preserve_base() {
        assert_eq!(keyword(Path::new("Alex Eala_001.jpg")), "Alex Eala");
        assert_eq!(keyword(Path::new("Alex Eala.jpg")), "Alex Eala");
    }
    #[test]
    fn image_only_covers_exact_audio_and_repeats_when_needed() {
        let shots = plan(&[image("one"), image("two")], 75.37, "", "", 0, 12).unwrap();
        validate(&shots, 75_370_000).unwrap();
        assert_eq!(shots.last().unwrap().end_us(), 75_370_000);
        assert!(shots.len() > 2);
    }
    #[test]
    fn video_segments_separated_and_unique() {
        let mut pool = vec![image("one"), image("two")];
        for n in 1..=15 {
            let mut item = image(&format!("clip{n}"));
            item.path = format!("/fake/video{n}.mp4");
            item.kind = "video".into(); item.duration = Some(15.0);
            pool.push(item);
        }
        let shots = plan(&pool, 160.0, "", "", 20, 31).unwrap();
        validate(&shots, 160_000_000).unwrap();
        assert!(shots.iter().any(|s| s.asset.kind == "video"));
    }
    #[test]
    fn persistent_cache_roundtrip_preserves_usage() {
        let root = std::env::temp_dir().join(format!("c-builder-cache-test-{}", random_seed()));
        fs::create_dir(&root).unwrap();
        let path = root.join("catalog.json");
        let mut cache = Cache::default();
        let mut asset = image("one");
        asset.used_count = 4;
        cache.entries.insert(asset.path.clone(), asset);
        save_cache(&path, &cache).unwrap();
        let loaded = read_cache(&path).unwrap();
        assert_eq!(loaded.entries["/fake/one.jpg"].used_count, 4);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn native_project_reopens_and_original_media_is_not_moved() {
        let root = std::env::temp_dir().join(format!("c-builder-native-test-{}", random_seed()));
        fs::create_dir(&root).unwrap();
        let picture = root.join("Eala 001.jpg");
        let narration = root.join("voice.wav");
        fs::write(&picture, b"fixture-image").unwrap();
        fs::write(&narration, b"fixture-audio").unwrap();
        let meta = fs::metadata(&picture).unwrap();
        let mut asset = image("one");
        asset.path = picture.to_string_lossy().into_owned();
        asset.size = meta.len();
        asset.modified_ns = stamp(&meta);
        let shots = plan(&[asset], 17.4, "", "", 0, 21).unwrap();
        let project_dir = write_native(&shots, &narration, 17.4, &root, "Demo").unwrap();
        let raw = fs::read_to_string(project_dir.join("concat.json")).unwrap();
        let document: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let reopened = doc::from_document(&document).unwrap();
        assert_eq!(reopened.active().clips.len(), shots.len() + 1);
        assert!(picture.exists());
        assert!(narration.exists());
        let second=write_native(&shots,&narration,17.4,&root,"Demo").unwrap();
        assert_ne!(second,project_dir);
        assert_eq!(second.file_name().unwrap(),"Demo (2)");
        assert_eq!(fs::read_to_string(project_dir.join("concat.json")).unwrap(),raw);
        let next:serde_json::Value=serde_json::from_slice(&fs::read(second.join("concat.json")).unwrap()).unwrap();
        assert_eq!(next["name"],"Demo (2)");
        assert_eq!(doc::from_document(&next).unwrap().active().name,"Demo (2)");
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn narration_name_defaults_manual_override_and_reset_are_independent_of_media() {
        let mut state=State::default();
        state.choose_narration_name(Path::new("/voice/s2.mp3"));assert_eq!(state.effective_name(),"s2");
        assert!(state.naming_preview().contains("s2.zip"));assert!(state.naming_preview().contains("s2.mp4"));
        state.choose_narration_name(Path::new("/voice/story.v2.wav"));assert_eq!(state.effective_name(),"story.v2");
        state.edit_project_name("Episode 03");state.choose_narration_name(Path::new("/voice/another.wav"));
        assert_eq!(state.effective_name(),"Episode 03");assert!(state.name_overridden);
        state.reset_project_name();assert_eq!(state.effective_name(),"another");assert!(!state.name_overridden);
    }
    #[test]
    fn archived_compatibility_paths_leave_inventory_without_reprobing_remaining_files() {
        let root=std::env::temp_dir().join(format!("c-builder-archive-cache-{}",random_seed()));
        let library=root.join("library");let outputs=root.join("projects");fs::create_dir_all(&library).unwrap();fs::create_dir(&outputs).unwrap();
        let library=library.canonicalize().unwrap();let voice=root.join("narration.wav");fs::write(&voice,b"voice").unwrap();
        let mut cache=Cache::default();let mut sources=Vec::new();
        for index in 0..2 {let path=library.join(format!("Eala {index}.jpg"));fs::write(&path,format!("source {index}")).unwrap();
            let mut asset=image("fixture");let m=fs::metadata(&path).unwrap();asset.path=path.to_string_lossy().into_owned();asset.size=m.len();asset.modified_ns=stamp(&m);cache.entries.insert(asset.path.clone(),asset.clone());sources.push(asset);}
        let cache_path=root.join("cache.json");save_cache(&cache_path,&cache).unwrap();
        let shots=vec![Shot{asset:sources[0].clone(),start_us:0,duration_us:1_000_000,trim_us:0}];
        let project=write_native(&shots,&voice,1.0,&outputs,"Story").unwrap();
        crate::c_builder_used::relocate_used(&library,&project,&[PathBuf::from(&sources[0].path)]).unwrap();
        let report=scan(&library,&cache_path,|_,_|{}).unwrap();assert_eq!(report.assets.len(),1);assert_eq!(report.probed,0);assert_eq!(report.reused,1);
        assert_eq!(report.assets[0].path,sources[1].path);assert!(!Path::new(&sources[0].path).exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn mixed_visual_sequence_reopens_on_one_track_with_separate_narration() {
        let root = std::env::temp_dir().join(format!("c-builder-mixed-test-{}", random_seed()));
        fs::create_dir(&root).unwrap();
        let still_path = root.join("Eala.jpg");
        let video_path = root.join("Eala.mp4");
        let narration = root.join("voice.wav");
        fs::write(&still_path, b"original-still").unwrap();
        fs::write(&video_path, b"original-video").unwrap();
        fs::write(&narration, b"original-audio").unwrap();
        let mut still = image("still");
        still.path = still_path.to_string_lossy().into_owned();
        let still_meta = fs::metadata(&still_path).unwrap();
        still.size = still_meta.len();
        still.modified_ns = stamp(&still_meta);
        let mut video = image("video");
        video.path = video_path.to_string_lossy().into_owned();
        video.kind = "video".into();
        video.duration = Some(12.0);
        video.video_codec = Some("h264".into());
        video.frame_rate = Some(30.0);
        video.frame_rate_fraction = Some("30/1".into());
        let video_meta = fs::metadata(&video_path).unwrap();
        video.size = video_meta.len();
        video.modified_ns = stamp(&video_meta);
        let shots = vec![
            Shot { asset: still.clone(), start_us: 0, duration_us: 3_000_000, trim_us: 0 },
            Shot { asset: video.clone(), start_us: 3_000_000, duration_us: 3_000_000,
                trim_us: 500_000 },
            Shot { asset: still, start_us: 6_000_000, duration_us: 4_000_000, trim_us: 0 },
        ];
        let project_dir = write_native(&shots, &narration, 10.0, &root, "Mixed").unwrap();
        let raw = fs::read_to_string(project_dir.join("concat.json")).unwrap();
        let saved: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let reopened = doc::from_document(&saved).unwrap();
        let clips = &reopened.active().clips;
        assert_eq!(clips.len(), 4);
        assert_eq!(clips[0].kind, ClipKind::Image);
        assert_eq!(clips[1].kind, ClipKind::Video);
        assert_eq!(clips[2].kind, ClipKind::Image);
        assert!(clips[..3].iter().all(|clip| clip.track_id == "T1"));
        assert_eq!(clips[3].track_id, "T3");
        assert_eq!(clips[3].kind, ClipKind::Audio);
        assert_eq!(clips[1].source_start, 0.5);
        assert_eq!(clips[1].start, 3.0);
        assert_eq!(clips[2].start, 6.0);
        assert_eq!(clips[3].duration, 10.0);
        assert!(reopened.media.iter().any(|media|
            media.path == video.path && media.video_codec.as_deref() == Some("h264")
                && media.frame_rate_fraction.as_deref() == Some("30/1")));
        assert!(still_path.exists() && video_path.exists() && narration.exists());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn slider_selection_excludes_unchecked_zero_and_disabled_video_groups() {
        use concat_project::planning::{KeywordSelection,MediaSelection,Profile};
        let mut first=image("first");first.keyword="Antonia".into();
        let mut other=image("other");other.keyword="Maria Sakkari".into();
        let mut video=first.clone();video.path="/fake/clip.mp4".into();video.kind="video".into();video.duration=Some(20.);
        let mut selection=MediaSelection::default();selection.images.insert("Antonia".into(),KeywordSelection {selected:true,weight:100});
        selection.images.insert("Maria Sakkari".into(),KeywordSelection {selected:false,weight:100});
        selection.videos.insert("Antonia".into(),KeywordSelection {selected:true,weight:100});
        let mut profile=Profile::default();profile.media_selection=Some(selection);
        let assets=[first,other,video];
        let(shots,_)=plan_profile(&assets,120.,"","",&profile).unwrap();
        assert!(shots.iter().all(|s|s.asset.keyword=="Antonia"&&s.asset.kind=="image"));
        profile.media_selection.as_mut().unwrap().images.get_mut("Antonia").unwrap().weight=0;
        assert!(plan_profile(&assets,120.,"","",&profile).is_err());
        assert_eq!(profile,Profile::from_json(&serde_json::to_string(&profile).unwrap()).unwrap());
    }
    #[test]
    fn keyword_slider_shares_do_not_depend_on_group_library_size() {
        use concat_project::planning::{Anchor,KeywordSelection,MediaSelection,Profile};
        let mut assets=Vec::new();
        for(group,count)in [("Antonia",113),("Maria Sakkari",10)] {for n in 0..count {let mut a=image(&format!("{group}{n}"));a.keyword=group.into();assets.push(a);}}
        let mut selection=MediaSelection::default();
        selection.images.insert("Antonia".into(),KeywordSelection {selected:true,weight:70});selection.images.insert("Maria Sakkari".into(),KeywordSelection {selected:true,weight:30});
        let mut profile=Profile::default();profile.curve=vec![Anchor {at:0.,video:0.},Anchor {at:1.,video:0.}];profile.media_selection=Some(selection);
        let(shots,_)=plan_profile(&assets,1200.,"","",&profile).unwrap();
        let ratio=shots.iter().filter(|s|s.asset.keyword=="Antonia").count() as f64/shots.len() as f64;
        assert!((ratio-0.70).abs()<0.10,"actual share {ratio}");
    }
    #[test]
    fn legacy_keyword_shares_preserve_planner_results_after_reopen() {
        use concat_project::planning::{KeywordSelection,MediaSelection,Profile};
        let mut assets=Vec::new();
        let mut selection=MediaSelection::default();
        for key in ["alpha","beta","gamma","delta"] {
            let mut a=image(key);
            a.keyword=key.into();
            assets.push(a);
            selection.images.insert(key.into(),KeywordSelection {selected:true,weight:100});
        }
        let mut profile=Profile::default();
        profile.media_selection=Some(selection);
        let (before,_)=plan_profile(&assets,120.,"","",&profile).unwrap();
        profile.media_selection.as_mut().unwrap().normalize();
        let saved=serde_json::to_string(&profile).unwrap();
        let reopened=Profile::from_json(&saved).unwrap();
        let (after,_)=plan_profile(&assets,120.,"","",&reopened).unwrap();
        assert_eq!(reopened.media_selection.as_ref().unwrap().images.values().map(|x|x.weight).collect::<Vec<_>>(),vec![25,25,25,25]);
        let identity=|shots:Vec<Shot>|shots.into_iter().map(|s|(s.asset.path,s.start_us,s.duration_us,s.trim_us)).collect::<Vec<_>>();
        assert_eq!(identity(before),identity(after));
    }
    #[test]
    fn validates_gaps() {
        let bad = vec![Shot { asset: image("one"), start_us: 2, duration_us: 1_000_000, trim_us: 0 }];
        assert!(validate(&bad, 1_000_002).is_err());
    }
}

/// Live desktop panel view; the cache itself is kept outside project files.
#[derive(Default)]
pub struct State {
    pub project_name:String,
    pub name_overridden:bool,
    pub auto_name:String,
    pub folder: String,
    pub assets: Vec<Asset>,
    pub keyword_summary: String,
    pub narration: String,
    pub duration: Option<f64>,
    pub status: String,
    pub scanning: bool,
    pub scan_done: usize,
    pub scan_total: usize,
    pub probed: usize,
    pub reused: usize,
    pub removed: usize,
    pub profile_json: String,
    // Session-local preferences are separate from active planner weights.
    pub remembered_image_weights: BTreeMap<String, u32>,
    pub remembered_video_weights: BTreeMap<String, u32>,
    pub preview: String,
    pub preview_shots: Vec<Shot>,
}
impl State {
    pub fn remembered_keyword_weight(&self, video: bool, keyword: &str) -> u32 {
        let weights = if video { &self.remembered_video_weights } else { &self.remembered_image_weights };
        weights.get(keyword).copied().unwrap_or(0)
    }

    pub fn forget_keyword_weight(&mut self, video: bool, keyword: &str) {
        let weights = if video { &mut self.remembered_video_weights } else { &mut self.remembered_image_weights };
        weights.remove(keyword);
    }

    /// Clear only the active keyword choices after a verified generation.
    /// The completed project's profile is already saved; this changes only the next draft.
    pub fn reset_keyword_selections_after_build(&mut self) -> Result<(), String> {
        let mut profile = if self.profile_json.trim().is_empty() {
            concat_project::planning::Profile::default()
        } else {
            concat_project::planning::Profile::from_json(&self.profile_json)?
        };
        let selection = profile.media_selection.get_or_insert_with(Default::default);
        selection.normalize();
        let images = std::mem::take(&mut selection.images);
        let videos = std::mem::take(&mut selection.videos);
        let encoded = serde_json::to_string_pretty(&profile).map_err(|e| e.to_string())?;
        self.remembered_image_weights.extend(images.into_iter().filter(|(_, row)| row.selected).map(|(key, row)| (key, row.weight)));
        self.remembered_video_weights.extend(videos.into_iter().filter(|(_, row)| row.selected).map(|(key, row)| (key, row.weight)));
        self.profile_json = encoded;
        self.preview_shots.clear();
        Ok(())
    }

    pub fn choose_narration_name(&mut self,path:&Path) {
        let next=concat_project::naming::audio_stem(path);
        if !self.name_overridden || self.project_name.trim().is_empty() {self.project_name=next.clone();}
        self.auto_name=next;
    }
    pub fn edit_project_name(&mut self,name:&str){self.project_name=name.to_owned();self.name_overridden=true;}
    pub fn reset_project_name(&mut self){self.project_name=self.auto_name.clone();self.name_overridden=false;}
    pub fn effective_name(&self)->String {concat_project::naming::basename(if self.project_name.trim().is_empty(){&self.auto_name}else{&self.project_name})}
    pub fn naming_preview(&self)->String {let name=self.effective_name();format!("Project: {name} · ZIP: {} · MP4: {} (duplicate names receive a numbered suffix)",concat_project::naming::zip_name(&name),concat_project::naming::mp4_name(&name))}

    pub fn images(&self) -> usize { self.assets.iter().filter(|a| a.kind == "image").count() }
    pub fn videos(&self) -> usize { self.assets.iter().filter(|a| a.kind == "video").count() }
    pub fn keywords(&self) -> String {
        let mut totals: BTreeMap<String, (usize, usize)> = BTreeMap::new();
        for a in &self.assets {
            let count = totals.entry(a.keyword.clone()).or_default();
            if a.kind == "image" { count.0 += 1; } else { count.1 += 1; }
        }
        totals.into_iter().map(|(k, (i, v))| format!("{k}: {i} images, {v} videos"))
            .collect::<Vec<_>>().join("\n")
    }
}

/// Simplify the internal configuration without changing the shared planner.
pub fn simplify_planning(p:&mut concat_project::planning::Profile){
    use concat_project::planning::Anchor;
    let opening=p.curve.first().map_or(0.65,|a|a.video);
    let ending=p.curve.last().map_or(0.05,|a|a.video);
    p.name="Timeline Planning".into();
    p.curve=vec![Anchor{at:0.,video:opening},Anchor{at:1.,video:ending}];
    p.pins.clear();p.ratio_by_seconds=true;
}
const PLANNING_SETTINGS:&str="c-builder-planning.json";
pub fn load_planning_settings(config:&Path)->concat_project::planning::Profile {
    let path=config.join(PLANNING_SETTINGS);
    let mut p=[path.clone(),path.with_extension("json.backup")].iter().filter_map(|p|fs::read_to_string(p).ok())
        .find_map(|s|concat_project::planning::Profile::from_json(&s).ok()).unwrap_or_default();
    simplify_planning(&mut p);p.media_selection=None;p.overall_video_percent=None;p
}
pub fn save_planning_settings(config:&Path,p:&concat_project::planning::Profile)->Result<(),String>{
    let mut p=p.clone();simplify_planning(&mut p);p.media_selection=None;p.overall_video_percent=None;p.validate()?;
    fs::create_dir_all(config).map_err(|e|e.to_string())?;
    let path=config.join(PLANNING_SETTINGS);
    let bytes=serde_json::to_vec_pretty(&p).map_err(|e|e.to_string())?;
    let pending = path.with_extension("json.pending");
    let backup = path.with_extension("json.backup");
    fs::write(&pending, bytes).map_err(|e| e.to_string())?;
    if backup.exists() && !path.exists(){fs::rename(&backup,&path).map_err(|e|e.to_string())?;}
    if backup.exists() { fs::remove_file(&backup).map_err(|e| e.to_string())?; }
    let existed = path.exists();
    if existed { fs::rename(&path, &backup).map_err(|e| e.to_string())?; }
    if let Err(e) = fs::rename(&pending, &path) {
        if existed { let _ = fs::rename(&backup, &path); }
        return Err(e.to_string());
    }
    if existed { let _ = fs::remove_file(backup); }
    Ok(())
}

#[cfg(test)]
mod planning_settings_tests {
    use super::*;
    #[test]
    fn configuration_survives_restart_without_project_selections(){
        let root=std::env::temp_dir().join(format!("concat-planning-settings-{}",random_seed()));
        let mut p=concat_project::planning::Profile::default();
        p.max_videos=4;p.image_seconds=[2.,6.];p.seed=42;p.media_selection=Some(Default::default());p.overall_video_percent=Some(30);
        save_planning_settings(&root,&p).unwrap();let saved=load_planning_settings(&root);
        assert_eq!(saved.max_videos,4);assert_eq!(saved.image_seconds,[2.,6.]);assert_eq!(saved.seed,42);
        assert_eq!(saved.curve.len(),2);assert!(saved.pins.is_empty());assert!(saved.media_selection.is_none());assert!(saved.overall_video_percent.is_none());
        p.image_seconds=[6.,2.];assert!(save_planning_settings(&root,&p).is_err());assert_eq!(load_planning_settings(&root),saved);
        fs::remove_dir_all(root).unwrap();
    }
}
