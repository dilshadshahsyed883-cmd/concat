// SPDX-License-Identifier: AGPL-3.0-or-later
//! Exact native drag/reorder plans shared by the preview and atomic commit.
//! No free-space relocation, media probing, rendering, or track inference.
//! Free targets are exact final coordinates. Ripple targets refer to the
//! original timeline under the pointer: removing an earlier source slot can
//! reduce the final coordinate; `destination_start` reports that coordinate
//! and the preview shows the exact result before a commit.
#![allow(missing_docs)]

use crate::commands::{self, ClipMove, Command, CommandError, IdMint, Outcome};
use crate::model::{Clip, ClipKind, Project, Track};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

const EPS: f64 = 1e-6;
const LINKS: [&str; 2] = ["gpuCaptionSource", "canvasOwner"];

#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DragMode {
    Free,
    Ripple,
}

#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DragRequest {
    pub moves: Vec<ClipMove>,
    pub mode: DragMode,
    #[serde(default)]
    pub duplicate: bool,
    #[serde(default)]
    pub magnetic: bool,
    #[serde(default)]
    pub locked_tracks: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct DragPreview {
    /// A read-only echo. Commit through `apply`/Command::DragClips, never assign this directly.
    pub project: Project,
    pub selected_ids: Vec<String>,
    pub created_ids: Vec<String>,
    pub destination_start: f64,
    pub ripple_count: usize,
}

fn refuse(reason: impl Into<String>) -> CommandError {
    CommandError::UnsafeEdit {
        reason: reason.into(),
    }
}
fn end(clip: &Clip) -> f64 {
    clip.start + clip.duration
}
fn parents(clip: &Clip) -> Vec<&str> {
    let mut ids = Vec::new();
    if let Some(id) = clip.detached_from.as_deref() {
        ids.push(id);
    }
    for key in LINKS {
        if let Some(id) = clip.extra.get(key).and_then(serde_json::Value::as_str) {
            if !ids.contains(&id) {
                ids.push(id);
            }
        }
    }
    ids
}
fn remap_parent(clip: &mut Clip, map: &HashMap<String, String>) {
    if let Some(id) = clip.detached_from.as_ref().and_then(|id| map.get(id)) {
        clip.detached_from = Some(id.clone());
    }
    for key in LINKS {
        if let Some(id) = clip
            .extra
            .get(key)
            .and_then(serde_json::Value::as_str)
            .and_then(|id| map.get(id))
            .cloned()
        {
            clip.extra.insert(key.into(), serde_json::Value::String(id));
        }
    }
}
fn locked(track: &Track, request: &DragRequest) -> bool {
    request.locked_tracks.contains(&track.id)
        || track
            .extra
            .get("locked")
            .and_then(serde_json::Value::as_bool)
            == Some(true)
}
fn typed_audio(track: &Track) -> bool {
    track
        .extra
        .get("audioOnly")
        .and_then(serde_json::Value::as_bool)
        == Some(true)
        || ["kind", "type", "mediaKind", "contentType"]
            .iter()
            .any(|key| {
                track
                    .extra
                    .get(*key)
                    .and_then(serde_json::Value::as_str)
                    .is_some_and(|v| {
                        matches!(
                            v.to_ascii_lowercase().as_str(),
                            "audio" | "audio-only" | "audio_only" | "sound"
                        )
                    })
            })
}
fn collisions(project: &Project, touched: Option<&HashSet<String>>) -> Result<(), CommandError> {
    let mut lanes: HashMap<&str, Vec<&Clip>> = HashMap::new();
    let mut ids = HashSet::new();
    for clip in &project.active().clips {
        if !ids.insert(&clip.id) {
            return Err(refuse("Duplicate clip identity in drag result"));
        }
        if !clip.start.is_finite()
            || clip.start < -EPS
            || !clip.duration.is_finite()
            || clip.duration <= 0.0
        {
            return Err(refuse("Invalid drag geometry"));
        }
        if touched.is_none_or(|tracks| tracks.contains(&clip.track_id)) {
            lanes.entry(&clip.track_id).or_default().push(clip);
        }
    }
    for lane in lanes.values_mut() {
        lane.sort_by(|a, b| a.start.total_cmp(&b.start));
        if lane.windows(2).any(|p| end(p[0]) > p[1].start + EPS) {
            return Err(refuse(
                "Drop would overlap another clip; choose Ripple mode or an empty slot",
            ));
        }
    }
    Ok(())
}

fn transition_seams(
    before: &Project,
    after: &Project,
    touched: &HashSet<String>,
) -> Result<(), CommandError> {
    let mut lanes: HashMap<&str, Vec<&Clip>> = HashMap::new();
    for c in &after.active().clips {
        if touched.contains(&c.track_id) {
            lanes.entry(&c.track_id).or_default().push(c);
        }
    }
    for lane in lanes.values_mut() {
        lane.sort_by(|a, b| a.start.total_cmp(&b.start));
        for (i, c) in lane.iter().enumerate() {
            let Some(cut) = c.transition_in.as_ref() else {
                continue;
            };
            let previous = i.checked_sub(1).map(|i| lane[i]);
            let old = before.active().clip(&c.id);
            let old_previous = old.and_then(|old| {
                before
                    .active()
                    .clips
                    .iter()
                    .filter(|p| {
                        p.id != old.id && p.track_id == old.track_id && p.start < old.start - EPS
                    })
                    .max_by(|a, b| a.start.total_cmp(&b.start))
            });
            let changed = old.is_none_or(|old| {
                old.track_id != c.track_id
                    || (old.start - c.start).abs() > EPS
                    || (old.duration - c.duration).abs() > EPS
            }) || old_previous.map(|p| p.id.as_str())
                != previous.map(|p| p.id.as_str())
                || old_previous
                    .zip(previous)
                    .is_some_and(|(old, new)| (old.duration - new.duration).abs() > EPS);
            if !changed {
                continue;
            }
            let previous=previous.ok_or_else(||refuse(format!("Transition on {} needs a contiguous preceding picture; adjust the cut before dragging",c.id)))?;
            if !c.kind.is_visual()
                || !previous.kind.is_visual()
                || (end(previous) - c.start).abs() > EPS
                || !cut.duration.is_finite()
                || cut.duration <= 0.0
                || cut.duration > c.duration.min(previous.duration) + EPS
            {
                return Err(refuse(format!(
                    "Drag would invalidate transition on {}; keep an adjacent picture and sufficient duration",
                    c.id
                )));
            }
        }
    }
    Ok(())
}

/// Calculate the exact echo using the same staged implementation as commit.
/// Geometry and links agree with commit; preview IDs are temporary because an
/// editor may have reserved higher IDs in undo history than the open document.
pub fn plan(project: &Project, request: &DragRequest) -> Result<DragPreview, CommandError> {
    let mut mint = IdMint::default();
    mint.adopt_project(project);
    calculate(project, &mut mint, request)
}

/// Commit one validated drag atomically, including ID allocation. The editor
/// wraps this in its normal snapshot, giving one Undo/Redo transaction.
pub fn apply(
    project: &mut Project,
    mint: &mut IdMint,
    request: DragRequest,
) -> Result<Outcome, CommandError> {
    let mut next_mint = mint.clone();
    let result = calculate(project, &mut next_mint, &request)?;
    let applied = result.project != *project;
    let created = request
        .duplicate
        .then(|| result.selected_ids.first().cloned())
        .flatten();
    *project = result.project;
    *mint = next_mint;
    Ok(Outcome {
        created_id: created,
        applied,
    })
}

fn calculate(
    project: &Project,
    mint: &mut IdMint,
    request: &DragRequest,
) -> Result<DragPreview, CommandError> {
    if request.moves.is_empty() || request.moves.len() > 10000 {
        return Err(refuse("Select 1–10000 clips for a drag"));
    }
    let timeline = project.active();
    let fps = timeline.video.rate();
    if !fps.is_finite() || fps <= 0.0 {
        return Err(refuse("Timeline frame rate is invalid"));
    }
    let quantize = |seconds: f64| (seconds * fps).round() / fps;
    let has_visual = request.moves.iter().any(|m| {
        timeline
            .clip(&m.clip_id)
            .is_some_and(|c| c.kind.is_visual())
    });
    let mut selected = HashSet::new();
    let mut moving = Vec::new();
    let mut targets = Vec::new();
    let mut delta: Option<f64> = None;
    let mut lane_delta: Option<isize> = None;
    for wanted in &request.moves {
        if !selected.insert(wanted.clip_id.clone()) {
            return Err(refuse("The drag selection contains a duplicate clip"));
        }
        if !wanted.start.is_finite() || wanted.start < 0.0 {
            return Err(CommandError::NotANumber);
        }
        let clip = timeline
            .clip(&wanted.clip_id)
            .ok_or(CommandError::MediaGone)?
            .clone();
        let from = timeline
            .tracks
            .iter()
            .position(|t| t.id == clip.track_id)
            .ok_or(CommandError::TrackGone)?;
        let to = timeline
            .tracks
            .iter()
            .position(|t| t.id == wanted.track_id)
            .ok_or(CommandError::TrackGone)?;
        if locked(&timeline.tracks[from], request) || locked(&timeline.tracks[to], request) {
            return Err(refuse("Drag touches a locked track"));
        }
        if clip.kind != ClipKind::Audio && typed_audio(&timeline.tracks[to]) {
            return Err(refuse(
                "Picture/text clips cannot be dropped on an explicitly audio-only track",
            ));
        }
        let shift = quantize(wanted.start) - quantize(clip.start);
        if delta.is_some_and(|d| (shift - d).abs() > EPS) {
            return Err(refuse(
                "A multi-selection drag must preserve relative timing",
            ));
        }
        delta = Some(shift);
        let row_shift = to as isize - from as isize;
        if !has_visual || clip.kind.is_visual() {
            if lane_delta.is_some_and(|d| d != row_shift) {
                return Err(refuse(
                    "A multi-track drag must preserve relative picture lanes",
                ));
            }
            lane_delta = Some(row_shift);
        } else if to != from {
            return Err(refuse(
                "Explicitly selected linked audio/caption lanes stay on their own tracks",
            ));
        }
        targets.push(ClipMove {
            clip_id: clip.id.clone(),
            track_id: wanted.track_id.clone(),
            start: quantize(wanted.start),
        });
        moving.push(clip);
    }
    // Both directions matter: moving a source alone is as unsafe as moving its sound alone.
    for clip in &timeline.clips {
        for parent_id in parents(clip) {
            if selected.contains(&clip.id) != selected.contains(parent_id) {
                return Err(refuse(
                    "Select linked source, audio, captions and canvas companions together",
                ));
            }
        }
    }
    let requested_start = targets
        .iter()
        .map(|m| m.start)
        .fold(f64::INFINITY, f64::min);
    if !request.duplicate
        && targets
            .iter()
            .zip(&moving)
            .all(|(m, c)| m.track_id == c.track_id && (m.start - c.start).abs() < EPS)
    {
        return Ok(DragPreview {
            project: project.clone(),
            selected_ids: moving.iter().map(|c| c.id.clone()).collect(),
            created_ids: Vec::new(),
            destination_start: requested_start,
            ripple_count: 0,
        });
    }
    let span = targets
        .iter()
        .zip(&moving)
        .map(|(m, c)| m.start + c.duration - requested_start)
        .fold(0.0, f64::max);
    if !span.is_finite() || span <= 0.0 {
        return Err(refuse("Drag selection has no positive duration"));
    }
    let destination_tracks: HashSet<String> = targets.iter().map(|m| m.track_id.clone()).collect();
    let mut staged = project.clone();
    let mut source_gaps: BTreeMap<String, Vec<(f64, f64)>> = BTreeMap::new();
    if !request.duplicate {
        staged
            .active_mut()
            .clips
            .retain(|c| !selected.contains(&c.id));
        if request.mode == DragMode::Ripple || request.magnetic {
            // Merge only the selected occupied intervals. Existing whitespace never becomes part of a closure.
            let mut occupied: BTreeMap<String, Vec<(f64, f64)>> = BTreeMap::new();
            for clip in &moving {
                occupied
                    .entry(clip.track_id.clone())
                    .or_default()
                    .push((clip.start, end(clip)));
            }
            for (track, mut intervals) in occupied {
                intervals.sort_by(|a, b| a.0.total_cmp(&b.0));
                let mut merged: Vec<(f64, f64)> = Vec::new();
                for (a, b) in intervals {
                    if let Some(last) = merged.last_mut().filter(|p| a <= p.1 + EPS) {
                        last.1 = last.1.max(b);
                    } else {
                        merged.push((a, b));
                    }
                }
                let first = timeline
                    .clips
                    .iter()
                    .filter(|c| c.track_id == track)
                    .map(|c| c.start)
                    .fold(f64::INFINITY, f64::min);
                for (a, b) in merged {
                    let left = (a - first).abs() < EPS
                        || timeline.clips.iter().any(|c| {
                            c.track_id == track
                                && !selected.contains(&c.id)
                                && (end(c) - a).abs() < EPS
                        });
                    let right = timeline.clips.iter().any(|c| {
                        c.track_id == track
                            && !selected.contains(&c.id)
                            && (c.start - b).abs() < EPS
                    });
                    if left && right {
                        source_gaps.entry(track.clone()).or_default().push((a, b));
                    }
                }
            }
            for c in &mut staged.active_mut().clips {
                if let Some(gaps) = source_gaps.get(&c.track_id) {
                    let shift: f64 = gaps
                        .iter()
                        .filter(|(_, b)| c.start >= *b - EPS)
                        .map(|(a, b)| b - a)
                        .sum();
                    if shift > 0.0 {
                        Arc::make_mut(c).start -= shift;
                    }
                }
            }
        }
    }
    let destination_start = if request.mode == DragMode::Ripple {
        let anchor_track = &targets
            .iter()
            .min_by(|a, b| a.start.total_cmp(&b.start))
            .ok_or_else(|| refuse("No drag target"))?
            .track_id;
        let mut adjusted = requested_start;
        if let Some(gaps) = source_gaps.get(anchor_track) {
            let mut removed = 0.0;
            for (a, b) in gaps {
                if requested_start >= *b - EPS {
                    removed += b - a;
                } else if requested_start > *a {
                    adjusted = *a;
                    break;
                }
            }
            adjusted -= removed;
        }
        quantize(adjusted.max(0.0))
    } else {
        requested_start
    };
    let mut created_ids = Vec::new();
    if request.mode == DragMode::Ripple {
        // Any linked stationary material affected by the ripple needs all its lanes in scope.
        for clip in &staged.active().clips {
            for parent_id in parents(clip) {
                if let Some(source) = staged.active().clip(parent_id) {
                    if destination_tracks.contains(&clip.track_id)
                        != destination_tracks.contains(&source.track_id)
                        && (end(clip) > destination_start + EPS
                            || end(source) > destination_start + EPS)
                    {
                        return Err(refuse(
                            "Ripple would desynchronize a linked track outside the selected drag scope",
                        ));
                    }
                }
            }
        }
        let split: Vec<Clip> = staged
            .active()
            .clips
            .iter()
            .filter(|c| {
                destination_tracks.contains(&c.track_id)
                    && c.start < destination_start - EPS
                    && end(c) > destination_start + EPS
            })
            .map(|c| c.as_ref().clone())
            .collect();
        for clip in &split {
            if clip.speed_curve.is_some() {
                return Err(refuse(
                    "Split a variable-speed destination clip explicitly before ripple insertion",
                ));
            }
            if destination_start - clip.start <= crate::model::ranges::MIN_CLIP_DURATION
                || end(clip) - destination_start <= crate::model::ranges::MIN_CLIP_DURATION
            {
                return Err(refuse("Ripple split would create a sub-frame fragment"));
            }
        }
        let before_ids: HashSet<_> = staged.active().clips.iter().map(|c| c.id.clone()).collect();
        commands::apply(
            &mut staged,
            mint,
            Command::SplitClips {
                clip_ids: split.iter().map(|c| c.id.clone()).collect(),
                time: destination_start,
            },
        )?;
        let mut tails = HashMap::new();
        for original in &split {
            if let Some(tail) = staged
                .active()
                .clips
                .iter()
                .find(|c| !before_ids.contains(&c.id) && c.track_id == original.track_id)
            {
                tails.insert(original.id.clone(), tail.id.clone());
                created_ids.push(tail.id.clone());
            }
        }
        for clip in &mut staged.active_mut().clips {
            if clip.start >= destination_start - EPS {
                remap_parent(Arc::make_mut(clip), &tails);
                if destination_tracks.contains(&clip.track_id) {
                    Arc::make_mut(clip).start += span;
                }
            }
        }
    }
    let mut remap = HashMap::new();
    let mut selected_ids = Vec::new();
    if request.duplicate {
        for clip in &moving {
            let id = mint.next("c");
            remap.insert(clip.id.clone(), id.clone());
            created_ids.push(id);
        }
    }
    for (original, target) in moving.iter().zip(&targets) {
        let mut clip = original.clone();
        if request.duplicate {
            clip.id = remap[&clip.id].clone();
            remap_parent(&mut clip, &remap);
        }
        clip.track_id = target.track_id.clone();
        clip.start = destination_start + (target.start - requested_start);
        selected_ids.push(clip.id.clone());
        staged.active_mut().clips.push(Arc::new(clip));
    }
    let mut touched = destination_tracks.clone();
    if !request.duplicate {
        touched.extend(moving.iter().map(|c| c.track_id.clone()));
    }
    collisions(&staged, Some(&touched))?;
    transition_seams(project, &staged, &touched)?;
    // Source-gap closure may affect stationary linked clips too. Refuse instead of silently pulling their source away.
    for old in &timeline.clips {
        for old_parent_id in parents(old) {
            if let (Some(after), Some(old_parent)) =
                (staged.active().clip(&old.id), timeline.clip(old_parent_id))
            {
                if parents(after).contains(&old_parent_id) {
                    let new_parent = staged
                        .active()
                        .clip(old_parent_id)
                        .ok_or_else(|| refuse("Drag lost a linked source"))?;
                    if ((after.start - old.start) - (new_parent.start - old_parent.start)).abs()
                        > EPS
                    {
                        return Err(refuse("Drag would desynchronize stationary linked media"));
                    }
                }
            }
        }
    }
    // A drop on either boundary of its own slot can remove/reinsert the
    // same block without changing the edit. Keep document order and undo clean.
    if !request.duplicate
        && staged.active().clips.len() == timeline.clips.len()
        && timeline
            .clips
            .iter()
            .all(|c| staged.active().clip(&c.id) == Some(c.as_ref()))
    {
        staged = project.clone();
    }
    // Preview and commit share this invariant: an unselected narration clip
    // must remain byte-for-byte the same, even if magnet/ripple touches its lane.
    if has_visual {
        for clip in timeline.clips.iter().filter(|c|
            c.kind == ClipKind::Audio && !selected.contains(&c.id))
        {
            if staged.active().clip(&clip.id) != Some(clip.as_ref()) {
                return Err(refuse("Visual drag would retime independent narration"));
            }
        }
    }
    let ripple_count = staged
        .active()
        .clips
        .iter()
        .filter(|c| !selected_ids.contains(&c.id))
        .filter(|c| {
            timeline
                .clip(&c.id)
                .is_some_and(|old| (old.start - c.start).abs() > EPS)
        })
        .count();
    Ok(DragPreview {
        project: staged,
        selected_ids,
        created_ids,
        destination_start,
        ripple_count,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Editor;
    use crate::doc::{DocumentSettings, to_document};
    use crate::model::{
        CaptionWord, ClipKey, KeyEase, KeyProperty, MediaItem, MediaKind, TextStyle,
    };

    fn settings() -> DocumentSettings {
        DocumentSettings {
            name: "Drag".into(),
            width: 1920,
            height: 1080,
            rate_num: 30,
            rate_den: 1,
        }
    }
    fn fixture() -> Project {
        let mut p = Project::new();
        p.media.push(MediaItem {
            id: "m1".into(),
            path: "original.mp4".into(),
            name: "Original".into(),
            kind: MediaKind::Video,
            duration: Some(100.0),
            ..Default::default()
        });
        for (id, start) in [("a", 0.0), ("b", 2.0), ("c", 4.0), ("d", 6.0)] {
            let mut clip = Clip::blank(id, "T1", ClipKind::Video, id, start, 2.0);
            clip.media_id = "m1".into();
            clip.source_start = 10.0;
            p.active_mut().clips.push(Arc::new(clip));
        }
        p
    }
    fn request(id: &str, start: f64, track: &str, mode: DragMode) -> DragRequest {
        DragRequest {
            moves: vec![ClipMove {
                clip_id: id.into(),
                start,
                track_id: track.into(),
            }],
            mode,
            duplicate: false,
            magnetic: true,
            locked_tracks: vec![],
        }
    }
    fn mint(p: &Project) -> IdMint {
        let mut mint = IdMint::default();
        mint.adopt_project(p);
        mint
    }

    #[test]
    fn free_drop_is_exact_and_collisions_are_atomic() {
        let mut p = fixture();
        let before = p.clone();
        let mut ids = mint(&p);
        let mut expected = ids.clone();
        assert!(apply(&mut p, &mut ids, request("b", 1.0, "T1", DragMode::Free)).is_err());
        assert_eq!(p, before);
        assert_eq!(ids.next("c"), expected.next("c"));
        apply(&mut p, &mut ids, request("b", 10.012, "T2", DragMode::Free)).unwrap();
        assert_eq!(p.active().clip("b").unwrap().start, 10.0);
        assert_eq!(p.active().clip("b").unwrap().track_id, "T2");
        assert_eq!(p.active().clip("c").unwrap().start, 2.0); // only the occupied source slot closed
    }
    #[test]
    fn free_magnet_off_keeps_intentional_gaps() {
        let mut p = fixture();
        let mut r = request("b", 15.0, "T2", DragMode::Free);
        r.magnetic = false;
        let mut ids = mint(&p);
        apply(&mut p, &mut ids, r).unwrap();
        assert_eq!(p.active().clip("c").unwrap().start, 4.0);
        assert_eq!(p.active().clip("b").unwrap().start, 15.0);
    }
    #[test]
    fn ripple_reorders_to_end_and_to_front_without_losing_ids() {
        for (id, time, expected) in [("b", 8.0, 6.0), ("a", 8.0, 6.0), ("c", 0.0, 0.0)] {
            let mut p = fixture();
            let mut ids = mint(&p);
            let old = p.active().clip(id).unwrap().clone();
            let preview = plan(&p, &request(id, time, "T1", DragMode::Ripple)).unwrap();
            apply(&mut p, &mut ids, request(id, time, "T1", DragMode::Ripple)).unwrap();
            assert_eq!(p, preview.project);
            let moved = p.active().clip(id).unwrap();
            assert_eq!(moved.start, expected);
            assert_eq!(moved.source_start, old.source_start);
            assert_eq!(moved.duration, old.duration);
            assert_eq!(p.active().clips.len(), 4);
            collisions(&p, None).unwrap();
        }
    }
    #[test]
    fn cross_track_insertion_splits_stationary_media_and_keys() {
        let mut p = fixture();
        let mut stationary = Clip::blank("target", "T2", ClipKind::Video, "Target", 0.0, 10.0);
        stationary.media_id = "m1".into();
        stationary.source_start = 3.0;
        stationary.set_key(KeyProperty::OffsetX, 0.0, 0.0, KeyEase::LINEAR);
        stationary.set_key(KeyProperty::OffsetX, 1.0, 1.0, KeyEase::LINEAR);
        p.active_mut().clips.push(Arc::new(stationary));
        let mut ids = mint(&p);
        apply(&mut p, &mut ids, request("b", 4.0, "T2", DragMode::Ripple)).unwrap();
        let head = p.active().clip("target").unwrap();
        assert_eq!(head.duration, 4.0);
        let tail = p
            .active()
            .clips
            .iter()
            .find(|c| c.track_id == "T2" && c.start == 6.0)
            .unwrap();
        assert_eq!(tail.source_start, 7.0);
        assert_eq!(tail.duration, 6.0);
        assert!(tail.transition_in.is_none());
        assert!(
            (head.value_at(KeyProperty::OffsetX, 1.0) - tail.value_at(KeyProperty::OffsetX, 0.0))
                .abs()
                < 1e-8
        );
        assert_eq!(p.active().clip("b").unwrap().start, 4.0);
    }
    #[test]
    fn multi_selection_preserves_relative_offsets_and_authored_edits() {
        let mut p = fixture();
        let mut r = request("a", 6.0, "T1", DragMode::Ripple);
        r.moves.push(ClipMove {
            clip_id: "b".into(),
            start: 8.0,
            track_id: "T1".into(),
        });
        let idx = p.active().clips.iter().position(|c| c.id == "a").unwrap();
        p.active_mut().clip_at_mut(idx).keys.push(ClipKey {
            property: KeyProperty::Scale,
            at: 0.5,
            value: 1.2,
            ease: KeyEase::IN_OUT,
        });
        let keys = p.active().clip("a").unwrap().keys.clone();
        let mut ids = mint(&p);
        apply(&mut p, &mut ids, r).unwrap();
        assert_eq!(
            p.active().clip("b").unwrap().start - p.active().clip("a").unwrap().start,
            2.0
        );
        assert_eq!(p.active().clip("a").unwrap().keys, keys);
    }

    #[test]
    fn free_and_ripple_drag_preserve_independent_locked_narration_and_undo() {
        for mode in [DragMode::Free, DragMode::Ripple] {
            for magnetic in [false, true] {
                let mut p = fixture();
                let mut narration = Clip::blank(
                    "voice", "T3", ClipKind::Audio, "Narration", 0.0, 50.0,
                );
                narration.source_start = 2.0;
                narration.media_id = "voice-media".into();
                p.media.push(MediaItem {
                    id: "voice-media".into(), path: "voice.wav".into(),
                    kind: MediaKind::Audio, duration: Some(60.0),
                    ..Default::default()
                });
                p.active_mut().clips.push(Arc::new(narration.clone()));
                let before = to_document(&settings(), &p);
                let mut editor = Editor::from_document(&before).unwrap();
                let mut move_request = request("b", 10.0, "T2", mode);
                move_request.magnetic = magnetic;
                move_request.locked_tracks.push("T3".into());
                editor.apply(Command::DragClips { request: move_request }).unwrap();
                assert_eq!(editor.project().active().clip("voice").unwrap(), &narration);
                let saved = editor.to_document(&settings());
                let reopened = Editor::from_document(&saved).unwrap();
                assert_eq!(reopened.project().active().clip("voice").unwrap(), &narration);
                assert!(editor.undo());
                assert_eq!(editor.to_document(&settings()), before);
                assert!(editor.redo());
                assert_eq!(editor.project().active().clip("voice").unwrap(), &narration);
            }
        }
    }

    #[test]
    fn explicit_audio_lane_and_locked_track_are_protected() {
        let mut p = fixture();
        let mut ids = mint(&p);
        let original = p.clone();
        let mut r = request("b", 10.0, "T2", DragMode::Free);
        r.locked_tracks.push("T1".into());
        assert!(apply(&mut p, &mut ids, r).is_err());
        let track = p
            .active_mut()
            .tracks
            .iter_mut()
            .find(|t| t.id == "T3")
            .unwrap();
        track
            .extra
            .insert("kind".into(), serde_json::json!("audio"));
        assert!(plan(&p, &request("b", 10.0, "T3", DragMode::Free)).is_err());
        p.active_mut()
            .tracks
            .iter_mut()
            .find(|t| t.id == "T3")
            .unwrap()
            .extra
            .clear();
        assert_eq!(p, original);
    }
    #[test]
    fn linked_audio_and_captions_require_explicit_selection_and_duplicate_remap() {
        let mut p = fixture();
        let mut audio = Clip::blank("sound", "T3", ClipKind::Audio, "Sound", 2.0, 2.0);
        audio.media_id = "m1".into();
        audio.detached_from = Some("b".into());
        let mut text = Clip::blank("caption", "T4", ClipKind::Text, "Word", 2.0, 2.0);
        text.extra
            .insert("gpuCaptionSource".into(), serde_json::json!("b"));
        p.active_mut()
            .clips
            .extend([Arc::new(audio), Arc::new(text)]);
        let mut r = request("b", 10.0, "T2", DragMode::Free);
        assert!(plan(&p, &r).is_err());
        r.moves.extend([
            ClipMove {
                clip_id: "sound".into(),
                track_id: "T3".into(),
                start: 10.0,
            },
            ClipMove {
                clip_id: "caption".into(),
                track_id: "T4".into(),
                start: 10.0,
            },
        ]);
        r.duplicate = true;
        let mut ids = mint(&p);
        let outcome = apply(&mut p, &mut ids, r).unwrap();
        let copy = p
            .active()
            .clip(outcome.created_id.as_deref().unwrap())
            .unwrap();
        assert_ne!(copy.id, "b");
        assert_eq!(copy.start, 10.0);
        let sound = p
            .active()
            .clips
            .iter()
            .find(|c| c.id != "sound" && c.kind == ClipKind::Audio)
            .unwrap();
        assert_eq!(sound.detached_from.as_deref(), Some(copy.id.as_str()));
        let caption = p
            .active()
            .clips
            .iter()
            .find(|c| c.id != "caption" && c.kind == ClipKind::Text)
            .unwrap();
        assert_eq!(caption.extra["gpuCaptionSource"], copy.id);
        assert_eq!(p.active().clip("b").unwrap().start, 2.0);
    }
    #[test]
    fn ripple_refuses_linked_stationary_lane_outside_scope() {
        let mut p = fixture();
        let mut sound = Clip::blank("sound", "T3", ClipKind::Audio, "Sound", 0.0, 2.0);
        sound.detached_from = Some("a".into());
        p.active_mut().clips.push(Arc::new(sound));
        let r = request("b", 0.0, "T1", DragMode::Ripple);
        assert!(plan(&p, &r).is_err());
    }
    #[test]
    fn caption_split_keeps_real_word_intervals_relative_to_each_piece() {
        let mut p = fixture();
        let mut c = Clip::blank("caption", "T2", ClipKind::Text, "First Last", 0.0, 10.0);
        c.text = Some(TextStyle {
            content: "First Last".into(),
            karaoke: true,
            karaoke_words: vec![
                CaptionWord {
                    text: "First".into(),
                    start_ms: 0,
                    end_ms: 4000,
                },
                CaptionWord {
                    text: "Last".into(),
                    start_ms: 4000,
                    end_ms: 10000,
                },
            ],
            ..TextStyle::default()
        });
        p.active_mut().clips.push(Arc::new(c));
        let mut ids = mint(&p);
        apply(&mut p, &mut ids, request("b", 4.0, "T2", DragMode::Ripple)).unwrap();
        let head = p.active().clip("caption").unwrap().text.as_ref().unwrap();
        assert_eq!(head.content, "First");
        assert_eq!(head.karaoke_words[0].end_ms, 4000);
        let tail = p
            .active()
            .clips
            .iter()
            .find(|c| c.kind == ClipKind::Text && c.id != "caption")
            .unwrap();
        assert_eq!(tail.start, 6.0);
        let words = &tail.text.as_ref().unwrap().karaoke_words;
        assert_eq!(words[0].text, "Last");
        assert_eq!(words[0].start_ms, 0);
    }
    #[test]
    fn undo_redo_and_save_reopen_preserve_same_planned_result() {
        let p = fixture();
        let mut e = Editor::from_document(&to_document(&settings(), &p)).unwrap();
        let r = request("b", 4.0, "T2", DragMode::Ripple);
        let preview = plan(e.project(), &r).unwrap();
        e.apply(Command::DragClips { request: r }).unwrap();
        assert_eq!(e.project(), &preview.project);
        let saved = e.to_document(&settings());
        assert_eq!(
            Editor::from_document(&saved).unwrap().project(),
            e.project()
        );
        assert!(e.undo());
        assert_eq!(e.project(), &p);
        assert!(e.redo());
        assert_eq!(e.project(), &preview.project);
    }
    #[test]
    fn thousand_clip_drop_is_exact_and_only_changes_requested_source_lane() {
        let mut p = Project::new();
        for i in 0..1000 {
            p.active_mut().clips.push(Arc::new(Clip::blank(
                format!("c{i}"),
                "T1",
                ClipKind::Image,
                "Still",
                i as f64 * 2.0,
                2.0,
            )));
        }
        let mut r = request("c500", 2500.0, "T2", DragMode::Free);
        r.magnetic = false;
        let result = plan(&p, &r).unwrap();
        assert_eq!(result.project.active().clips.len(), 1000);
        assert_eq!(result.project.active().clip("c500").unwrap().start, 2500.0);
        assert_eq!(result.project.active().clip("c501").unwrap().start, 1002.0);
    }
    #[test]
    fn no_motion_has_no_undo_or_id_allocation_and_request_round_trips() {
        let mut p = fixture();
        let r = request("b", 2.0, "T1", DragMode::Free);
        let json = serde_json::to_string(&r).unwrap();
        assert_eq!(serde_json::from_str::<DragRequest>(&json).unwrap(), r);
        let mut ids = mint(&p);
        let original = p.clone();
        let result = apply(&mut p, &mut ids, r).unwrap();
        assert!(!result.applied);
        assert_eq!(p, original);
    }
}

#[cfg(test)]
mod seam_tests {
    use super::*;
    use crate::model::Transition;
    fn project() -> Project {
        let mut p = Project::new();
        for (id, start) in [("a", 0.0), ("b", 2.0), ("c", 4.0), ("d", 6.0)] {
            p.active_mut().clips.push(Arc::new(Clip::blank(
                id,
                "T1",
                ClipKind::Image,
                id,
                start,
                2.0,
            )));
        }
        p.active_mut().clip_at_mut(1).transition_in = Some(Transition {
            id: "concat.fade-black".into(),
            duration: 0.5,
            intensity: Some(0.7),
            params: Default::default(),
        });
        p
    }
    fn request(moves: Vec<ClipMove>, mode: DragMode) -> DragRequest {
        DragRequest {
            moves,
            mode,
            duplicate: false,
            magnetic: false,
            locked_tracks: vec![],
        }
    }
    fn target(id: &str, start: f64, track: &str) -> ClipMove {
        ClipMove {
            clip_id: id.into(),
            start,
            track_id: track.into(),
        }
    }
    #[test]
    fn free_gap_cannot_orphan_an_authored_transition() {
        let p = project();
        assert!(plan(&p, &request(vec![target("b", 10.0, "T2")], DragMode::Free)).is_err());
    }
    #[test]
    fn an_intact_pair_retains_its_transition_and_stationary_successor_is_guarded() {
        let p = project();
        let result = plan(
            &p,
            &request(
                vec![target("a", 10.0, "T2"), target("b", 12.0, "T2")],
                DragMode::Free,
            ),
        )
        .unwrap();
        assert_eq!(
            result.project.active().clip("b").unwrap().transition_in,
            p.active().clip("b").unwrap().transition_in
        );
        assert!(plan(&p, &request(vec![target("a", 10.0, "T2")], DragMode::Free)).is_err());
    }
    #[test]
    fn unrelated_overlapping_overlay_lane_is_preserved() {
        let mut p = project();
        p.active_mut().clips.push(Arc::new(Clip::blank(
            "title1",
            "T4",
            ClipKind::Text,
            "One",
            0.0,
            10.0,
        )));
        p.active_mut().clips.push(Arc::new(Clip::blank(
            "title2",
            "T4",
            ClipKind::Text,
            "Two",
            5.0,
            10.0,
        )));
        let result = plan(&p, &request(vec![target("d", 10.0, "T2")], DragMode::Free)).unwrap();
        assert_eq!(
            result.project.active().clip("title1"),
            p.active().clip("title1")
        );
        assert_eq!(
            result.project.active().clip("title2"),
            p.active().clip("title2")
        );
    }
    #[test]
    fn drop_on_own_head_or_tail_is_a_true_noop() {
        let p = project();
        for time in [2.0, 4.0] {
            let mut staged = p.clone();
            let mut ids = IdMint::default();
            ids.adopt_project(&p);
            let r = request(vec![target("b", time, "T1")], DragMode::Ripple);
            let result = apply(&mut staged, &mut ids, r).unwrap();
            assert!(!result.applied);
            assert_eq!(staged, p);
        }
    }
}
