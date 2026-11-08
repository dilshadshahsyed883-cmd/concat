// SPDX-License-Identifier: AGPL-3.0-or-later
//! C Builder: persistent, non-destructive media catalogue and native Concat timeline generation.
//! All expensive probes happen on the caller's background worker, never during timeline generation.
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
struct Rng(u64);
impl Rng {
    fn new(seed: u64) -> Self { Self(if seed == 0 { 0x9e3779b97f4a7c15 } else { seed }) }
    fn next(&mut self) -> u64 {
        let mut x = self.0; x ^= x << 13; x ^= x >> 7; x ^= x << 17; self.0 = x; x
    }
    fn up_to(&mut self, limit: u64) -> u64 { if limit == 0 { 0 } else { self.next() % limit } }
}
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
fn choose<'a>(pool: &[&'a Asset], weights: &HashMap<String, u32>,
    counts: &mut HashMap<String, u32>, recent: &[String], used: &HashSet<String>,
    unique: bool, rng: &mut Rng) -> Option<&'a Asset> {
    let mut candidates: Vec<&Asset> = pool.iter().copied().filter(|a|
        weights.contains_key(&a.keyword) && (!unique || !used.contains(&a.path))).collect();
    if candidates.len() > 1 {
        let other: Vec<&Asset> = candidates.iter().copied()
            .filter(|a| !recent.contains(&a.path)).collect();
        if !other.is_empty() { candidates = other; }
    }
    if candidates.is_empty() { return None; }
    let denominator: f64 = weights.values().map(|n| f64::from(*n)).sum();
    let picked: f64 = counts.values().map(|n| f64::from(*n)).sum();
    let min_use = candidates.iter().map(|a| a.used_count).min().unwrap_or(0);
    let mut best = f64::NEG_INFINITY; let mut options: Vec<&Asset> = Vec::new();
    for a in candidates {
        let deficit = (picked + 1.0) * f64::from(weights[&a.keyword]) / denominator
            - f64::from(*counts.get(&a.keyword).unwrap_or(&0));
        let score = deficit - 0.01 * f64::from(a.used_count.saturating_sub(min_use));
        if score > best + 0.000001 { best = score; options.clear(); }
        if score >= best - 0.000001 { options.push(a); }
    }
    let chosen = options[rng.up_to(options.len() as u64) as usize];
    *counts.entry(chosen.keyword.clone()).or_default() += 1;
    Some(chosen)
}
pub fn plan(assets: &[Asset], seconds: f64, image_weights: &str, video_weights: &str,
    coverage: u32, seed: u64) -> Result<Vec<Shot>, String> {
    if !seconds.is_finite() || seconds <= 0.0 || seconds > 86_400.0 {
        return Err("Narration duration must be between 0 and 24 hours".into());
    }
    if coverage > 40 { return Err("Video coverage must be 0–40%".into()); }
    let total = (seconds * 1_000_000.0).round() as u64;
    let image_pool: Vec<&Asset> = assets.iter().filter(|a| a.kind == "image").collect();
    let image_weights = strengths(image_weights, &image_pool)?;
    let video_pool: Vec<&Asset> = assets.iter().filter(|a|
        a.kind == "video" && a.duration.is_some_and(|d| d >= 2.5)).collect();
    let video_weights = if coverage > 0 { strengths(video_weights, &video_pool)? }
        else { HashMap::new() };
    let mut rng = Rng::new(seed);
    let mut videos = Vec::new(); let mut video_count = HashMap::new();
    let mut used = HashSet::new(); let mut recent_video = Vec::new();
    let target = (seconds * f64::from(coverage) / 100.0 / 3.25).round() as usize;
    // Never plan clips that cannot have an image-only interval on both sides.
    let maximum = (seconds / 6.0).floor() as usize;
    for _ in 0..target.min(video_pool.len()).min(maximum) {
        let Some(a) = choose(&video_pool, &video_weights, &mut video_count,
            &recent_video, &used, true, &mut rng) else { break };
        used.insert(a.path.clone()); recent_video = vec![a.path.clone()];
        let source_seconds = a.duration.unwrap_or(2.5);
        let duration = 2_500_000 + rng.up_to(1_500_001);
        let duration = duration.min((source_seconds * 1_000_000.0) as u64);
        let trim_max = (source_seconds * 1_000_000.0) as u64 - duration;
        videos.push(((*a).clone(), duration, rng.up_to(trim_max + 1)));
    }
    let video_duration: u64 = videos.iter().map(|v| v.1).sum();
    let gaps = videos.len() + 1;
    let minimum_gap = if videos.is_empty() { 0 } else { 3_000_000 };
    if video_duration + minimum_gap * gaps as u64 >= total {
        return Err("Insufficient image-only room for chosen video coverage".into());
    }
    let extra = total - video_duration - minimum_gap * gaps as u64;
    let weights: Vec<u64> = (0..gaps).map(|_| 70 + rng.up_to(61)).collect();
    let sum: u64 = weights.iter().sum();
    let mut sizes: Vec<u64> = weights.iter().map(|w| minimum_gap + extra * w / sum).collect();
    sizes[gaps - 1] += total - video_duration - sizes.iter().sum::<u64>();
    let mut result = Vec::new(); let mut cursor = 0; let mut image_counts = HashMap::new();
    let mut recent_images: Vec<String> = Vec::new();
    for (index, gap) in sizes.into_iter().enumerate() {
        let mut remaining = gap;
        while remaining > 0 {
            let duration = remaining.min(3_000_000 + rng.up_to(2_000_001));
            let a = choose(&image_pool, &image_weights, &mut image_counts,
                &recent_images, &HashSet::new(), false, &mut rng)
                .ok_or("Unable to choose image for gap")?;
            recent_images.push(a.path.clone());
            if recent_images.len() > 3 { recent_images.remove(0); }
            result.push(Shot { asset: (*a).clone(), start_us: cursor,
                duration_us: duration, trim_us: 0 });
            cursor += duration; remaining -= duration;
        }
        if let Some((asset, duration, trim)) = videos.get(index) {
            result.push(Shot { asset: asset.clone(), start_us: cursor,
                duration_us: *duration, trim_us: *trim });
            cursor += *duration;
        }
    }
    validate(&result, total)?;
    Ok(result)
}
pub fn validate(shots: &[Shot], total: u64) -> Result<(), String> {
    if shots.is_empty() { return Err("Timeline has no visual media".into()); }
    let mut cursor = 0;
    let mut seen_videos = HashSet::new();
    let mut previous_video = false;
    for shot in shots {
        if shot.start_us != cursor || shot.duration_us == 0 {
            return Err("Timeline contains a gap or overlap".into());
        }
        if shot.asset.kind == "video" {
            if previous_video || !seen_videos.insert(&shot.asset.path) {
                return Err("Consecutive or repeated video clips".into());
            }
            if !(2_500_000..=4_000_000).contains(&shot.duration_us) {
                return Err("Video duration outside 2.5–4 seconds".into());
            }
            if shot.asset.duration.unwrap_or(0.0) * 1_000_000.0 + 1.0
                < (shot.trim_us + shot.duration_us) as f64 {
                return Err("Video trim exceeds source".into());
            }
        } else if shot.duration_us > 5_000_000 {
            return Err("Image duration exceeds 5 seconds".into());
        }
        previous_video = shot.asset.kind == "video";
        cursor = shot.end_us();
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
            let mut clip = Clip::blank(format!("c{}", i + 1),
                if is_image { "T1" } else { "T2" },
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
