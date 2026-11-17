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
    if existing { fs::rename(path, &backup).map_err(|e| e.to_string())?; }
    if let Err(e) = fs::rename(&tmp, path) {
        if existing { let _ = fs::rename(&backup, path); }
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
    let files = collect(&root)?;
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

    let name = name.trim();
    if name.is_empty() || name == "." || name == ".."
        || name.chars().any(|c| "\\/:*?\"<>|".contains(c)) {
        return Err("Enter a valid project name".into());
    }
    let total = (duration * 1_000_000.0).round() as u64;
    validate(shots, total)?;
    let root = parent.join(format!("C Builder - {name}"));
    // Never overwrite projects (or any existing folder). Source media is never moved.
    fs::create_dir(&root).map_err(|e| format!("Project already exists or cannot be created: {e}"))?;
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
        assert!(write_native(&shots, &narration, 17.4, &root, "Demo").is_err());
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
    fn validates_gaps() {
        let bad = vec![Shot { asset: image("one"), start_us: 2, duration_us: 1_000_000, trim_us: 0 }];
        assert!(validate(&bad, 1_000_002).is_err());
    }
}

/// Live desktop panel view; the cache itself is kept outside project files.
#[derive(Default)]
pub struct State {
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
    pub preview: String,
    pub preview_shots: Vec<Shot>,
    pub stored_profiles: Vec<concat_project::planning::Profile>,
    pub active_profile_name: String,
}
impl State {
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

/// Validated profile persistence with Windows-safe replacement and rollback.
pub fn save_profile(path: &Path, source: &str) -> Result<(), String> {
    let profile = concat_project::planning::Profile::from_json(source)?;
    let bytes = serde_json::to_vec_pretty(&profile).map_err(|e| e.to_string())?;
    let pending = path.with_extension("json.pending");
    let backup = path.with_extension("json.backup");
    fs::write(&pending, bytes).map_err(|e| e.to_string())?;
    if backup.exists() { fs::remove_file(&backup).map_err(|e| e.to_string())?; }
    let existed = path.exists();
    if existed { fs::rename(path, &backup).map_err(|e| e.to_string())?; }
    if let Err(e) = fs::rename(&pending, path) {
        if existed { let _ = fs::rename(&backup, path); }
        return Err(e.to_string());
    }
    if existed { let _ = fs::remove_file(backup); }
    Ok(())
}

/// Managed profile filenames never interpret a profile name as a filesystem path.
pub fn profile_file(folder:&Path,name:&str)->PathBuf {
    let hash=name.bytes().fold(0xcbf29ce484222325_u64,|h,b|(h^u64::from(b)).wrapping_mul(0x100000001b3));
    folder.join(format!("profile-{hash:016x}.json"))
}
pub fn load_profiles(folder:&Path)->Vec<concat_project::planning::Profile> {
    let mut profiles=Vec::new();
    if let Ok(entries)=fs::read_dir(folder){for entry in entries.flatten().take(500){
        if entry.file_type().is_ok_and(|t|t.is_file())&&entry.path().extension().is_some_and(|e|e=="json"){
            if let Ok(source)=fs::read_to_string(entry.path()){
                if let Ok(profile)=concat_project::planning::Profile::from_json(&source){profiles.push(profile);}
            }
        }
    }}
    profiles.sort_by(|a,b|a.name.cmp(&b.name));profiles
}
pub fn store_profile(folder:&Path,source:&str)->Result<(),String> {
    let profile=concat_project::planning::Profile::from_json(source)?;
    fs::create_dir_all(folder).map_err(|e|e.to_string())?;
    let path=profile_file(folder,&profile.name);
    if path.is_symlink(){return Err("Managed profile cannot be a symbolic link".into());}
    if path.exists(){let old=concat_project::planning::Profile::from_json(&fs::read_to_string(&path).map_err(|e|e.to_string())?)?;
        if old.name!=profile.name{return Err("Profile filename collision; choose another name".into());}}
    save_profile(&path,source)
}

#[cfg(test)]
mod profile_tests {
    use super::*;
    #[test]
    fn managed_profiles_replace_atomically_and_preserve_sources() {
        let root=std::env::temp_dir().join(format!("concat-profile-test-{}",random_seed()));
        let mut profile=concat_project::planning::Profile::default();profile.name="Custom / profile".into();
        let mut json=serde_json::to_string(&profile).unwrap();store_profile(&root,&json).unwrap();
        profile.max_videos=3;json=serde_json::to_string(&profile).unwrap();store_profile(&root,&json).unwrap();
        let profiles=load_profiles(&root);assert_eq!(profiles.len(),1);assert_eq!(profiles[0].max_videos,3);
        assert!(save_profile(&profile_file(&root,&profile.name),"{}").is_err());
        assert_eq!(load_profiles(&root)[0].max_videos,3);
        std::fs::remove_dir_all(root).unwrap();
    }
}
