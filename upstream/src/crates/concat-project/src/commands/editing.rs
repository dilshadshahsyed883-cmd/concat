// SPDX-License-Identifier: AGPL-3.0-or-later
//! Explicit timeline edits, staged atomically before touching the open project.
use super::*;
trait ClipEnd {
    fn end(&self) -> f64;
}
impl ClipEnd for Clip {
    fn end(&self) -> f64 {
        self.start + self.duration
    }
}

fn refused(reason: &str) -> CommandError {
    CommandError::UnsafeEdit {
        reason: reason.into(),
    }
}

/// A narration lane can be marked audio-only, or simply contain sound.
/// Never place a visual clip there, even during a gap in the waveform.
pub(super) fn audio_lane(timeline: &Timeline, id: &str) -> bool {
    timeline
        .clips
        .iter()
        .any(|c| c.track_id == id && c.kind == ClipKind::Audio)
        || timeline.track(id).is_some_and(|track| {
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
                            .is_some_and(|kind| {
                                matches!(
                                    kind.to_ascii_lowercase().as_str(),
                                    "audio" | "audio-only" | "audio_only" | "sound"
                                )
                            })
                    })
        })
}
fn scope(project: &Project, tracks: &[String], locked: &[String]) -> Result<(), CommandError> {
    if tracks.is_empty() {
        return Err(refused("Select at least one track"));
    }
    for id in tracks {
        if project.active().track(id).is_none() {
            return Err(CommandError::TrackGone);
        }
        if locked.contains(id) {
            return Err(refused("Selected scope contains a locked track"));
        }
    }
    Ok(())
}
fn links(project: &Project, tracks: &[String], from: f64) -> Result<(), CommandError> {
    let timeline = project.active();
    for clip in &timeline.clips {
        if clip.end() <= from {
            continue;
        }
        if let Some(parent) = clip.detached_from.as_ref().and_then(|id| timeline.clip(id)) {
            if tracks.contains(&clip.track_id) != tracks.contains(&parent.track_id) {
                return Err(refused(
                    "Include both linked audio and video tracks to preserve sync",
                ));
            }
        }
        for key in ["gpuCaptionSource", "canvasOwner"] {
            if let Some(parent) = clip
                .extra
                .get(key)
                .and_then(serde_json::Value::as_str)
                .and_then(|id| timeline.clip(id))
            {
                if tracks.contains(&clip.track_id) != tracks.contains(&parent.track_id) {
                    return Err(refused(
                        "Include source and linked caption/canvas tracks in the ripple scope",
                    ));
                }
            }
        }
    }
    Ok(())
}

pub(super) fn apply(
    project: &mut Project,
    mint: &mut IdMint,
    command: Command,
) -> Result<Outcome, CommandError> {
    let mut staged = project.clone();
    let outcome = match command {
        Command::InsertMedia {
            media_id,
            track_id,
            mut tracks,
            locked_tracks,
            time,
            overwrite,
            linked_audio,
        } => {
            let media = staged
                .media_by_id(&media_id)
                .ok_or(CommandError::MediaGone)?
                .clone();
            if media.kind != MediaKind::Audio && !linked_audio {
                if audio_lane(staged.active(), &track_id) {
                    return Err(refused("Cannot insert visual media onto a narration track"));
                }
                // UI selection can contain narration; selection alone never
                // opts that sound into a visual ripple or split.
                tracks.retain(|id| !audio_lane(staged.active(), id));
            }
            scope(&staged, &tracks, &locked_tracks)?;
            if !tracks.contains(&track_id) {
                return Err(refused("Destination must be in the selected scope"));
            }
            links(&staged, &tracks, time)?;
            let duration = super::clips::clip_length(&media);
            if !duration.is_finite() || duration < MIN_CLIP_DURATION {
                return Err(refused("Invalid source duration"));
            }
            let rate = staged.active().video;
            let fps = rate.rate();
            let time = (time.max(0.0) * fps).round() / fps;
            let cut_ids: Vec<_> = staged
                .active()
                .clips
                .iter()
                .filter(|c| tracks.contains(&c.track_id) && c.start < time && c.end() > time)
                .map(|c| c.id.clone())
                .collect();
            // Do not silently flatten a speed curve or round away a very short head/tail.
            for id in &cut_ids {
                let c = staged.active().clip(id).ok_or(CommandError::MediaGone)?;
                if c.speed_curve.is_some()
                    || time - c.start <= MIN_CLIP_DURATION
                    || c.end() - time <= MIN_CLIP_DURATION
                {
                    return Err(refused(
                        "Insert touches a speed curve or a sub-frame fragment; split/adjust the clip first",
                    ));
                }
            }
            let originals: Vec<_> = cut_ids
                .iter()
                .filter_map(|id| staged.active().clip(id).cloned())
                .collect();
            super::apply(
                &mut staged,
                mint,
                Command::SplitClips {
                    clip_ids: cut_ids,
                    time,
                },
            )?;
            let mut tails = std::collections::HashMap::new();
            for original in &originals {
                if let Some(tail) = staged.active().clips.iter().find(|c| {
                    c.id != original.id
                        && c.track_id == original.track_id
                        && (c.start - time).abs() < 1e-8
                }) {
                    tails.insert(original.id.clone(), tail.id.clone());
                }
            }
            for tail in staged
                .active_mut()
                .clips_where(|c| (c.start - time).abs() < 1e-8)
            {
                if let Some(parent) = tail.detached_from.as_ref().and_then(|id| tails.get(id)) {
                    tail.detached_from = Some(parent.clone());
                }
            }
            if overwrite {
                let end = time + duration;
                let end_ids = staged
                    .active()
                    .clips
                    .iter()
                    .filter(|c| c.track_id == track_id && c.start < end && c.end() > end)
                    .map(|c| c.id.clone())
                    .collect();
                super::apply(
                    &mut staged,
                    mint,
                    Command::SplitClips {
                        clip_ids: end_ids,
                        time: end,
                    },
                )?;
                // Overwrite is explicit and affects only the destination lane.
                staged
                    .active_mut()
                    .clips
                    .retain(|c| !(c.track_id == track_id && c.start >= time && c.start < end));
            } else {
                for c in staged
                    .active_mut()
                    .clips_where(|c| tracks.contains(&c.track_id) && c.start >= time)
                {
                    c.start += duration;
                }
            }
            let id = mint.next("c");
            staged
                .active_mut()
                .clips
                .push(Arc::new(super::clips::default_clip(
                    id.clone(),
                    track_id,
                    &media,
                    time,
                )));
            Outcome {
                created_id: Some(id),
                applied: true,
            }
        }
        Command::ReplaceMediaFromFile {
            clip_id,
            item,
            use_source_duration,
            keep_edits,
            tracks,
            locked_tracks,
            linked_audio,
        } => {
            let media_id = if let Some(existing) = staged.media.iter().find(|m| m.path == item.path)
            {
                existing.id.clone()
            } else {
                super::apply(&mut staged, mint, Command::AddMedia { item })?
                    .created_id
                    .ok_or(CommandError::MediaGone)?
            };
            apply(
                &mut staged,
                mint,
                Command::ReplaceMediaInSlot {
                    clip_id,
                    media_id,
                    use_source_duration,
                    keep_edits,
                    tracks,
                    locked_tracks,
                    linked_audio,
                },
            )?
        }
        Command::ReplaceMediaInSlot {
            clip_id,
            media_id,
            use_source_duration,
            keep_edits,
            mut tracks,
            locked_tracks,
            linked_audio,
        } => {
            if !linked_audio {
                tracks.retain(|id| !audio_lane(staged.active(), id));
            }
            scope(&staged, &tracks, &locked_tracks)?;
            let before = staged
                .active()
                .clip(&clip_id)
                .ok_or(CommandError::MediaGone)?
                .clone();
            if !tracks.contains(&before.track_id) {
                return Err(refused("Clip is outside selected scope"));
            }
            if !before.kind.is_visual() {
                return Err(refused("Select an image or video clip"));
            }
            let media = staged
                .media_by_id(&media_id)
                .ok_or(CommandError::MediaGone)?
                .clone();
            if media.kind == MediaKind::Audio {
                return Err(refused("Choose an image or video"));
            }
            let new_duration = if use_source_duration {
                super::clips::clip_length(&media)
            } else {
                before.duration
            };
            let source_span = if keep_edits {
                new_duration * before.speed.abs()
            } else {
                new_duration
            };
            if !new_duration.is_finite()
                || new_duration < MIN_CLIP_DURATION
                || (media.kind == MediaKind::Video
                    && media
                        .duration
                        .is_none_or(|d| !d.is_finite() || d + 1e-6 < source_span))
            {
                return Err(refused(
                    "New video is shorter than the slot; choose source-duration ripple or a longer source",
                ));
            }
            if before.speed_curve.is_some() && keep_edits {
                return Err(refused(
                    "Reset edits before replacing a variable-speed clip",
                ));
            }
            let delta = new_duration - before.duration;
            if delta != 0.0 {
                links(&staged, &tracks, before.start)?;
            }
            // Refuse a ripple that would collide with a spanning clip on another scoped lane.
            if delta != 0.0
                && staged.active().clips.iter().any(|c| {
                    tracks.contains(&c.track_id)
                        && c.id != clip_id
                        && c.start < before.end()
                        && c.end() > before.end()
                })
            {
                return Err(refused(
                    "A scoped clip spans the ripple boundary; split it first",
                ));
            }
            let mut after = if keep_edits {
                before.clone()
            } else {
                super::clips::default_clip(
                    before.id.clone(),
                    before.track_id.clone(),
                    &media,
                    before.start,
                )
            };
            after.media_id = media_id;
            after.kind = if media.kind == MediaKind::Image {
                ClipKind::Image
            } else {
                ClipKind::Video
            };
            after.duration = new_duration;
            after.source_start = 0.0;
            after.name = media.name;
            // Source-derived cached processing cannot be reused on unrelated media.
            after.enhanced = None;
            after.stabilize = None;
            after.cutout = None;
            if after.kind == ClipKind::Image {
                after.audio_stream = None;
            }
            for c in &mut staged.active_mut().clips {
                if c.id == clip_id {
                    *c = Arc::new(after.clone());
                } else if tracks.contains(&c.track_id) && c.start >= before.end() {
                    Arc::make_mut(c).start += delta;
                }
            }
            Outcome {
                created_id: Some(clip_id),
                applied: true,
            }
        }
        Command::CloseTrackGaps {
            track_id,
            locked_tracks,
            at,
        } => {
            scope(&staged, std::slice::from_ref(&track_id), &locked_tracks)?;
            // Gap closing is a visual lane operation. Linked audio needs an explicit multi-track edit.
            links(&staged, std::slice::from_ref(&track_id), 0.0)?;
            let mut ordered: Vec<_> = staged
                .active()
                .clips
                .iter()
                .filter(|c| c.track_id == track_id)
                .cloned()
                .collect();
            ordered.sort_by(|a, b| a.start.total_cmp(&b.start));
            if ordered.iter().any(|c| !c.kind.is_visual()) {
                return Err(refused("Gap close is restricted to a visual track"));
            }
            let mut commands = Vec::new();
            let mut shift = 0.0;
            for pair in ordered.windows(2) {
                let gap = pair[1].start - pair[0].end();
                // Preserve the intentional intro; selected gap is the one under the playhead.
                if gap > 1e-6 && at.is_none_or(|t| t >= pair[0].end() && t < pair[1].start) {
                    shift += gap;
                }
                if shift > 0.0 {
                    commands.push(ClipMove {
                        clip_id: pair[1].id.clone(),
                        track_id: track_id.clone(),
                        start: pair[1].start - shift,
                    });
                }
            }
            super::apply(&mut staged, mint, Command::MoveClips { moves: commands })?
        }
        Command::SetPlanningProfile { profile } => {
            crate::planning::Profile::from_json(&profile.to_string()).map_err(|e| refused(&e))?;
            let previous = staged
                .extra
                .insert("cBuilderPlanning".into(), profile.clone());
            Outcome {
                created_id: None,
                applied: previous.as_ref() != Some(&profile),
            }
        }
        Command::SetCaptionPresetSettings { settings } => {
            let obj = settings.as_object().ok_or_else(|| refused("Invalid caption preset configuration"))?;
            let id = obj.get("presetId").and_then(serde_json::Value::as_str)
                .ok_or_else(|| refused("Caption preset ID missing"))?;
            let style = obj.get("style").and_then(serde_json::Value::as_object)
                .ok_or_else(|| refused("Caption style missing"))?;
            let position = obj.get("position").and_then(serde_json::Value::as_object)
                .ok_or_else(|| refused("Caption position missing"))?;
            if obj.get("version").and_then(serde_json::Value::as_u64) != Some(1)
                || !id.starts_with("cuda-caption.")
                || id.len() > 128
                || obj.get("generateOnGpu").and_then(serde_json::Value::as_bool) != Some(true)
                || style.contains_key("content") || style.contains_key("karaokeWords")
                || position.get("anchor").and_then(serde_json::Value::as_str) != Some("bottom-center")
                || !position.get("offsetY").and_then(serde_json::Value::as_f64)
                    .is_some_and(|y| y.is_finite() && (0.0..=0.45).contains(&y))
            {
                return Err(refused("Unsupported caption preset configuration"));
            }
            let previous = staged.extra.insert("captionPreset".into(), settings.clone());
            Outcome { created_id: None, applied: previous.as_ref() != Some(&settings) }
        }
        Command::SetGpuCaptionRequest { request } => {
            if let Some(value) = &request {
                let object = value
                    .as_object()
                    .ok_or_else(|| refused("Invalid GPU caption request"))?;
                if object.get("version").and_then(serde_json::Value::as_u64) != Some(1)
                    || object.get("model").and_then(serde_json::Value::as_str) != Some("small")
                    || object
                        .get("wordTimestamps")
                        .and_then(serde_json::Value::as_bool)
                        != Some(true)
                    || !matches!(
                        object.get("preset").and_then(serde_json::Value::as_str),
                        Some("purple-word" | "yellow-bar" | "classic"
                            | "cuda-caption.classic-subtitle"
                            | "cuda-caption.bold-highlight"
                            | "cuda-caption.cinematic-title"
                            | "cuda-caption.minimal-clean"
                            | "cuda-caption.news-alert"
                            | "cuda-caption.modern-accent"
                            | "cuda-caption.karaoke-highlight"
                            | "cuda-caption.preset-1-purple-word"
                            | "cuda-caption.preset-2-yellow-bar")
                    )
                {
                    return Err(refused(
                        "Unsupported GPU caption request version/model/preset",
                    ));
                }
                let id = object
                    .get("sourceClipId")
                    .and_then(serde_json::Value::as_str)
                    .ok_or_else(|| refused("Select a narration clip"))?;
                let source = staged.active().clip(id).ok_or(CommandError::MediaGone)?;
                if !matches!(source.kind, ClipKind::Audio | ClipKind::Video) {
                    return Err(refused("GPU captions need audio or video"));
                }
            }
            let previous = match request.clone() {
                Some(value) => staged.extra.insert("gpuCaptionRequest".into(), value),
                None => staged.extra.remove("gpuCaptionRequest"),
            };
            Outcome {
                created_id: None,
                applied: previous != request,
            }
        }
        Command::ImportGpuCaptions { artifact } => {
            let artifact: crate::gpu_captions::Artifact =
                serde_json::from_value(artifact).map_err(|e| refused(&e.to_string()))?;
            let source = staged
                .active()
                .clip(&artifact.source_clip_id)
                .ok_or(CommandError::MediaGone)?;
            if (source.start * 1000. - artifact.timeline_start_ms as f64).abs() > 1.
                || artifact
                    .cues
                    .iter()
                    .any(|c| c.end_ms as f64 > source.duration * 1000. + 1.)
            {
                return Err(refused(
                    "Caption source timing changed since this result was generated",
                ));
            }
            let commands = artifact.commands().map_err(|e| refused(&e))?;
            // Import exact speech timing, then restore the user's saved look
            // rather than replacing nine gallery styles with a legacy default.
            let native_id = match artifact.preset.as_str() {
                "yellow-bar" => "cuda-caption.preset-2-yellow-bar",
                "purple-word" => "cuda-caption.preset-1-purple-word",
                "classic" => "cuda-caption.classic-subtitle",
                other => other,
            };
            let saved_preset = staged.extra.get("captionPreset")
                .filter(|settings| settings.get("presetId")
                    .and_then(serde_json::Value::as_str) == Some(native_id));
            let saved_style = saved_preset
                .and_then(|settings| settings.get("style"))
                .and_then(|value| serde_json::from_value::<TextStyle>(value.clone()).ok());
            let saved_offset_y = saved_preset
                .and_then(|settings| settings.get("position"))
                .and_then(|position| position.get("offsetY"))
                .and_then(serde_json::Value::as_f64)
                .filter(|y| y.is_finite() && (0.0..=0.45).contains(y));
            staged.active_mut().clips.retain(|c| {
                c.extra
                    .get("gpuCaptionSource")
                    .and_then(serde_json::Value::as_str)
                    != Some(artifact.source_clip_id.as_str())
            });
            let mut created = None;
            for command in commands {
                let outcome = super::apply(&mut staged, mint, command)?;
                if let Some(id) = &outcome.created_id {
                    if let Some(index) = staged.active().clips.iter().position(|c| &c.id == id) {
                        let clip = staged.active_mut().clip_at_mut(index);
                        clip.extra.insert(
                            "gpuCaptionSource".into(),
                            serde_json::Value::String(artifact.source_clip_id.clone()),
                        );
                        if let (Some(saved), Some(generated)) =
                            (saved_style.as_ref(), clip.text.as_ref()) {
                            let mut styled = saved.clone();
                            styled.content = generated.content.clone();
                            styled.karaoke_words = generated.karaoke_words.clone();
                            styled.karaoke_word = None;
                            // Never show fake karaoke alignment without true word times.
                            if styled.karaoke_words.is_empty() {
                                styled.karaoke = false;
                                styled.karaoke_background.clear();
                            }
                            clip.text = Some(styled);
                        }
                        if let Some(y) = saved_offset_y {
                            clip.offset_y = y;
                        }
                    }
                }
                created = outcome.created_id;
            }
            staged.extra.remove("gpuCaptionRequest");
            Outcome {
                created_id: created,
                applied: true,
            }
        }
        Command::ScopedMoveClips {
            mut moves,
            locked_tracks,
            magnetic,
        } => {
            let originals = staged.active().clips.clone();
            let mut gaps = Vec::new();
            for wanted in moves.clone() {
                let before = staged
                    .active()
                    .clip(&wanted.clip_id)
                    .ok_or(CommandError::MediaGone)?;
                if locked_tracks.contains(&before.track_id)
                    || locked_tracks.contains(&wanted.track_id)
                {
                    return Err(refused("Drag touches a locked track"));
                }
                let delta = wanted.start - before.start;
                if let Some(parent) = before
                    .detached_from
                    .as_deref()
                    .or_else(|| {
                        before
                            .extra
                            .get("gpuCaptionSource")
                            .and_then(serde_json::Value::as_str)
                    })
                    .and_then(|id| staged.active().clip(id))
                {
                    if delta.abs() > 1e-8
                        && !moves.iter().any(|m| {
                            m.clip_id == parent.id && (m.start - parent.start - delta).abs() < 1e-6
                        })
                    {
                        return Err(refused(
                            "Select the linked source before moving its sound/captions",
                        ));
                    }
                }
                for linked in &originals {
                    if linked
                        .extra
                        .get("canvasOwner")
                        .and_then(serde_json::Value::as_str)
                        == Some(before.id.as_str())
                    {
                        if wanted.track_id != before.track_id {
                            return Err(refused(
                                "Reapply canvas after moving its foreground to another lane",
                            ));
                        }
                        if locked_tracks.contains(&linked.track_id) {
                            return Err(refused("A canvas companion is locked"));
                        }
                        if !moves.iter().any(|m| m.clip_id == linked.id) {
                            moves.push(ClipMove {
                                clip_id: linked.id.clone(),
                                track_id: linked.track_id.clone(),
                                start: linked.start + delta,
                            });
                        }
                    }
                    let parent = linked.detached_from.as_deref().or_else(|| {
                        linked
                            .extra
                            .get("gpuCaptionSource")
                            .and_then(serde_json::Value::as_str)
                    });
                    if parent == Some(before.id.as_str())
                        && delta.abs() > 1e-8
                        && !moves.iter().any(|m| {
                            m.clip_id == linked.id && (m.start - linked.start - delta).abs() < 1e-6
                        })
                    {
                        return Err(refused(
                            "Select linked sound/captions before moving their source",
                        ));
                    }
                }
                if magnetic
                    && before.kind.is_visual()
                    && !before.extra.contains_key("canvasOwner")
                    && originals.iter().any(|c| {
                        c.track_id == before.track_id
                            && (c.start + c.duration - before.start).abs() < 1e-6
                    })
                    && originals.iter().any(|c| {
                        c.track_id == before.track_id && (c.start - before.end()).abs() < 1e-6
                    })
                {
                    gaps.push((before.track_id.clone(), before.start));
                }
            }
            let mut outcome = super::apply(&mut staged, mint, Command::MoveClips { moves })?;
            for (track_id, at) in gaps {
                let closed = apply(
                    &mut staged,
                    mint,
                    Command::CloseTrackGaps {
                        track_id,
                        locked_tracks: locked_tracks.clone(),
                        at: Some(at),
                    },
                )?;
                outcome.applied |= closed.applied;
            }
            outcome
        }
        Command::SetTimelineMagnetic { enabled } => {
            let old = staged
                .active_mut()
                .extra
                .insert("magnetic".into(), serde_json::Value::Bool(enabled));
            Outcome {
                created_id: None,
                applied: old != Some(serde_json::Value::Bool(enabled)),
            }
        }
        _ => unreachable!("editing commands only"),
    };
    *project = staged;
    Ok(outcome)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::editor::Editor;
    fn settings() -> crate::doc::DocumentSettings {
        crate::doc::DocumentSettings {
            name: "Test".into(),
            width: 1920,
            height: 1080,
            rate_num: 30,
            rate_den: 1,
        }
    }
    fn fixture() -> Editor {
        let mut project = Project::new();
        project.media.push(MediaItem {
            id: "m1".into(),
            name: "Video".into(),
            path: "video.mp4".into(),
            duration: Some(10.0),
            kind: MediaKind::Video,
            ..Default::default()
        });
        project.media.push(MediaItem {
            id: "m2".into(),
            name: "Photo".into(),
            path: "photo.png".into(),
            kind: MediaKind::Image,
            ..Default::default()
        });
        for (id, start) in [("c1", 0.0), ("c2", 10.0)] {
            let mut c = Clip::blank(id, "T1", ClipKind::Video, "Original", start, 10.0);
            c.media_id = "m1".into();
            project.active_mut().clips.push(Arc::new(c));
        }
        Editor::from_document(&crate::doc::to_document(&settings(), &project)).unwrap()
    }
    fn insert(time: f64, locked: bool) -> Command {
        Command::InsertMedia {
            media_id: "m2".into(),
            track_id: "T1".into(),
            tracks: vec!["T1".into()],
            locked_tracks: if locked { vec!["T1".into()] } else { vec![] },
            time,
            overwrite: false,
            linked_audio: false,
        }
    }
    #[test]
    fn insert_inside_is_source_continuous_and_one_undo() {
        let mut e = fixture();
        e.apply(insert(4.0, false)).unwrap();
        let t = e.project().active();
        assert_eq!(t.clips.len(), 4);
        let tail = t.clips.iter().find(|c| c.start == 9.0).unwrap();
        assert_eq!(tail.source_start, 4.0);
        assert_eq!(tail.duration, 6.0);
        assert_eq!(t.clip("c2").unwrap().start, 15.0);
        assert!(e.undo());
        assert_eq!(e.project().active().clips.len(), 2);
        assert!(e.redo());
    }
    #[test]
    fn rejected_edits_are_atomic() {
        let mut e = fixture();
        let before = e.to_document(&settings());
        assert!(e.apply(insert(4.0, true)).is_err());
        assert!(e.apply(insert(f64::NAN, false)).is_err());
        assert_eq!(before, e.to_document(&settings()));
    }
    #[test]
    fn replace_and_gap_close_keep_slot_and_intro() {
        let mut e = fixture();
        e.apply(Command::ReplaceMediaInSlot {
            clip_id: "c1".into(),
            media_id: "m2".into(),
            use_source_duration: false,
            keep_edits: true,
            tracks: vec!["T1".into()],
            locked_tracks: vec![],
            linked_audio: false,
        })
        .unwrap();
        assert_eq!(e.project().active().clip("c1").unwrap().duration, 10.0);
        assert_eq!(
            e.project().active().clip("c1").unwrap().kind,
            ClipKind::Image
        );
        e.apply(Command::MoveClips {
            moves: vec![
                ClipMove {
                    clip_id: "c1".into(),
                    track_id: "T1".into(),
                    start: 3.0,
                },
                ClipMove {
                    clip_id: "c2".into(),
                    track_id: "T1".into(),
                    start: 20.0,
                },
            ],
        })
        .unwrap();
        e.apply(Command::CloseTrackGaps {
            track_id: "T1".into(),
            locked_tracks: vec![],
            at: None,
        })
        .unwrap();
        assert_eq!(e.project().active().clip("c1").unwrap().start, 3.0);
        assert_eq!(e.project().active().clip("c2").unwrap().start, 13.0);
        e.apply(Command::SetTimelineMagnetic { enabled: true })
            .unwrap();
        let saved = e.to_document(&settings());
        let reopened = Editor::from_document(&saved).unwrap();
        assert_eq!(reopened.project().active().extra["magnetic"], true);
    }
}

#[cfg(test)]
mod safety_tests {
    use super::*;
    #[test]
    fn scope_guards_linked_tracks_and_preserves_tail_links() {
        let mut p = Project::new();
        p.media.push(MediaItem {
            id: "m1".into(),
            kind: MediaKind::Video,
            duration: Some(10.0),
            ..Default::default()
        });
        let mut video = Clip::blank("v1", "T1", ClipKind::Video, "V", 0.0, 10.0);
        video.media_id = "m1".into();
        let mut audio = Clip::blank("a1", "T3", ClipKind::Audio, "A", 0.0, 10.0);
        audio.media_id = "m1".into();
        audio.detached_from = Some("v1".into());
        p.active_mut().clips = vec![Arc::new(video), Arc::new(audio)];
        let mut mint = IdMint::default();
        mint.adopt_project(&p);
        let cmd = |tracks, linked_audio| Command::InsertMedia {
            media_id: "m1".into(),
            track_id: "T1".into(),
            tracks,
            locked_tracks: vec![],
            time: 4.0,
            overwrite: false,
            linked_audio,
        };
        assert!(apply(&mut p, &mut mint, cmd(vec!["T1".into()], false)).is_err());
        assert_eq!(p.active().clips.len(), 2);
        assert!(
            apply(
                &mut p,
                &mut mint,
                cmd(vec!["T1".into(), "T3".into()], false)
            )
            .is_err()
        );
        assert_eq!(p.active().clips.len(), 2);
        apply(&mut p, &mut mint, cmd(vec!["T1".into(), "T3".into()], true)).unwrap();
        let vt = p
            .active()
            .clips
            .iter()
            .find(|c| c.track_id == "T1" && c.start == 14.0)
            .unwrap();
        let at = p
            .active()
            .clips
            .iter()
            .find(|c| c.track_id == "T3" && c.start == 14.0)
            .unwrap();
        assert_eq!(at.detached_from.as_deref(), Some(vt.id.as_str()));
        assert_eq!(vt.source_start, 4.0);
        assert_eq!(at.source_start, 4.0);
    }
    #[test]
    fn selected_gap_and_many_clips_are_non_destructive() {
        let mut p = Project::new();
        for i in 0..1000 {
            p.active_mut().clips.push(Arc::new(Clip::blank(
                format!("c{i}"),
                "T1",
                ClipKind::Image,
                "still",
                3.0 + i as f64 * 2.0,
                1.0,
            )));
        }
        let mut mint = IdMint::default();
        mint.adopt_project(&p);
        apply(
            &mut p,
            &mut mint,
            Command::CloseTrackGaps {
                track_id: "T1".into(),
                locked_tracks: vec![],
                at: Some(4.5),
            },
        )
        .unwrap();
        assert_eq!(p.active().clip("c0").unwrap().start, 3.0);
        assert_eq!(p.active().clip("c1").unwrap().start, 4.0);
        assert_eq!(p.active().clip("c999").unwrap().start, 2000.0);
        apply(
            &mut p,
            &mut mint,
            Command::CloseTrackGaps {
                track_id: "T1".into(),
                locked_tracks: vec![],
                at: None,
            },
        )
        .unwrap();
        assert_eq!(p.active().clip("c999").unwrap().start, 1002.0);
    }
}

#[cfg(test)]
mod interaction_tests {
    use super::*;
    use crate::{DocumentSettings, Editor};
    fn settings() -> DocumentSettings {
        DocumentSettings {
            name: "Interactions".into(),
            width: 1920,
            height: 1080,
            rate_num: 30,
            rate_den: 1,
        }
    }
    #[test]
    fn magnetic_drag_closes_only_the_new_internal_gap_and_is_one_undo() {
        for magnetic in [true, false] {
            let mut p = Project::new();
            p.media.push(MediaItem {
                id: "m1".into(),
                path: "still.jpg".into(),
                kind: MediaKind::Image,
                ..Default::default()
            });
            for i in 0..3 {
                let mut shot = Clip::blank(
                    format!("c{i}"),
                    "T1",
                    ClipKind::Image,
                    "shot",
                    3.0 + i as f64 * 5.0,
                    5.0,
                );
                shot.media_id = "m1".into();
                p.active_mut().clips.push(Arc::new(shot));
            }
            let mut e = Editor::from_document(&crate::doc::to_document(&settings(), &p)).unwrap();
            e.apply(Command::ScopedMoveClips {
                moves: vec![ClipMove {
                    clip_id: "c1".into(),
                    track_id: "T1".into(),
                    start: 30.0,
                }],
                locked_tracks: vec![],
                magnetic,
            })
            .unwrap();
            assert_eq!(e.project().active().clip("c0").unwrap().start, 3.0);
            assert_eq!(
                e.project().active().clip("c2").unwrap().start,
                if magnetic { 8.0 } else { 13.0 }
            );
            assert!(e.undo());
            assert_eq!(e.project().active().clip("c2").unwrap().start, 13.0);
            assert_eq!(e.project().active().clip("c1").unwrap().start, 8.0);
        }
    }
    #[test]
    fn caption_import_is_idempotent_and_clears_recognition_request() {
        let mut p = Project::new();
        let mut voice = Clip::blank("voice", "T3", ClipKind::Audio, "voice", 2.0, 3.0);
        voice.media_id = "m1".into();
        p.media.push(MediaItem {
            id: "m1".into(),
            path: "voice.wav".into(),
            kind: MediaKind::Audio,
            duration: Some(3.0),
            ..Default::default()
        });
        p.active_mut().clips.push(Arc::new(voice));
        let mut mint = IdMint::default();
        mint.adopt_project(&p);
        let artifact = serde_json::json!({"version":1,"sourceClipId":"voice","timelineStartMs":2000,"preset":"yellow-bar","cues":[{"text":"Hello","startMs":0,"endMs":1000,"words":[{"text":"Hello","startMs":0,"endMs":1000}]}]});
        for _ in 0..2 {
            apply(
                &mut p,
                &mut mint,
                Command::ImportGpuCaptions {
                    artifact: artifact.clone(),
                },
            )
            .unwrap();
            assert_eq!(p.active().clips.len(), 2);
        }
        assert!(!p.extra.contains_key("gpuCaptionRequest"));
    }
}

#[cfg(test)]
mod visual_audio_protection_tests {
    use super::*;
    use crate::{DocumentSettings, Editor};

    fn settings() -> DocumentSettings {
        DocumentSettings {
            name: "Narration regression".into(),
            width: 1920,
            height: 1080,
            rate_num: 30,
            rate_den: 1,
        }
    }

    fn fixture() -> Editor {
        let mut project = Project::new();
        project.media.push(MediaItem {
            id: "video".into(),
            kind: MediaKind::Video,
            path: "visual.mp4".into(),
            duration: Some(10.0),
            ..Default::default()
        });
        project.media.push(MediaItem {
            id: "image".into(),
            kind: MediaKind::Image,
            path: "still.png".into(),
            ..Default::default()
        });
        project.media.push(MediaItem {
            id: "voice-media".into(),
            kind: MediaKind::Audio,
            path: "voice.wav".into(),
            duration: Some(70.0),
            ..Default::default()
        });
        let mut v1 = Clip::blank("v1", "T1", ClipKind::Video, "visual1", 0.0, 10.0);
        v1.media_id = "video".into();
        let mut v2 = Clip::blank("v2", "T1", ClipKind::Video, "visual2", 10.0, 10.0);
        v2.media_id = "video".into();
        let mut narration = Clip::blank("voice", "T3", ClipKind::Audio, "Narration", 0.0, 60.0);
        narration.media_id = "voice-media".into();
        narration.source_start = 0.5;
        project
            .active_mut()
            .clips
            .extend([Arc::new(v1), Arc::new(v2), Arc::new(narration)]);
        Editor::from_document(&crate::doc::to_document(&settings(), &project)).unwrap()
    }

    fn voice(editor: &Editor) -> Clip {
        editor.project().active().clip("voice").unwrap().clone()
    }

    #[test]
    fn insert_at_playhead_preserves_narration_in_ripple_overwrite_and_locked_modes() {
        for media in ["image", "video"] {
            for overwrite in [false, true] {
                let mut editor = fixture();
                let narration = voice(&editor);
                let before = editor.to_document(&settings());
                editor
                    .apply(Command::InsertMedia {
                        media_id: media.into(),
                        track_id: "T1".into(),
                        // A stale UI selection may include narration and even its locked lane.
                        tracks: vec!["T1".into(), "T3".into()],
                        locked_tracks: vec!["T3".into()],
                        time: 4.0,
                        overwrite,
                        linked_audio: false,
                    })
                    .unwrap();
                assert_eq!(voice(&editor), narration);
                assert_eq!(
                    editor
                        .project()
                        .active()
                        .clips
                        .iter()
                        .filter(|c| c.kind == ClipKind::Audio)
                        .count(),
                    1
                );
                assert!(
                    editor
                        .project()
                        .active()
                        .clips
                        .iter()
                        .any(|c| c.track_id == "T1" && c.start == 4.0 && c.media_id == media)
                );
                let saved = editor.to_document(&settings());
                let reopened = Editor::from_document(&saved).unwrap();
                assert_eq!(voice(&reopened), narration);
                assert!(editor.undo());
                assert_eq!(editor.to_document(&settings()), before);
                assert!(editor.redo());
                assert_eq!(voice(&editor), narration);
                assert_eq!(editor.to_document(&settings()), saved);
            }
        }
    }

    #[test]
    fn visual_replace_and_magnet_moves_leave_waveform_and_id_untouched() {
        for magnetic in [false, true] {
            let mut editor = fixture();
            let narration = voice(&editor);
            editor
                .apply(Command::SetTimelineMagnetic { enabled: magnetic })
                .unwrap();
            editor
                .apply(Command::ReplaceMediaInSlot {
                    clip_id: "v1".into(),
                    media_id: "image".into(),
                    use_source_duration: true,
                    keep_edits: false,
                    tracks: vec!["T1".into(), "T3".into()],
                    locked_tracks: vec!["T3".into()],
                    linked_audio: false,
                })
                .unwrap();
            assert_eq!(voice(&editor), narration);
            editor
                .apply(Command::ScopedMoveClips {
                    moves: vec![ClipMove {
                        clip_id: "v2".into(),
                        track_id: "T2".into(),
                        start: 22.0,
                    }],
                    locked_tracks: vec!["T3".into()],
                    magnetic,
                })
                .unwrap();
            assert_eq!(voice(&editor), narration);
            assert!(editor.undo());
            assert_eq!(voice(&editor), narration);
            assert!(editor.redo());
            assert_eq!(voice(&editor), narration);
        }
    }

    #[test]
    fn visual_drop_cannot_use_audio_lane_but_first_free_avoids_it() {
        let mut editor = fixture();
        let narration = voice(&editor);
        let before = editor.to_document(&settings());
        for command in [
            Command::AddClip {
                media_id: "image".into(),
                track_id: "T3".into(),
                start: 3.0,
                ripple: true,
            },
            Command::AddClipsInSequence {
                media_ids: vec!["image".into(), "video".into()],
                track_id: Some("T3".into()),
                start: 3.0,
                ripple: true,
            },
            Command::InsertMedia {
                media_id: "image".into(),
                track_id: "T3".into(),
                tracks: vec!["T3".into()],
                locked_tracks: vec![],
                time: 3.0,
                overwrite: false,
                linked_audio: false,
            },
        ] {
            assert!(editor.apply(command).is_err());
            assert_eq!(editor.to_document(&settings()), before);
        }
        let inserted = editor
            .apply(Command::AddClipAtFirstFree {
                media_id: "image".into(),
                start: 30.0,
            })
            .unwrap()
            .created_id
            .unwrap();
        assert_ne!(
            editor.project().active().clip(&inserted).unwrap().track_id,
            "T3"
        );
        assert_eq!(voice(&editor), narration);
    }

    #[test]
    fn visual_batch_ripple_only_retimes_its_own_track() {
        let mut editor = fixture();
        let narration = voice(&editor);
        editor
            .apply(Command::AddClipsInSequence {
                media_ids: vec!["image".into(), "video".into()],
                track_id: Some("T1".into()),
                start: 10.0,
                ripple: true,
            })
            .unwrap();
        assert_eq!(voice(&editor), narration);
        assert_eq!(
            editor
                .project()
                .active()
                .clips
                .iter()
                .filter(|c| c.kind == ClipKind::Audio)
                .count(),
            1
        );
        let saved = editor.to_document(&settings());
        assert_eq!(voice(&Editor::from_document(&saved).unwrap()), narration);
        assert!(editor.undo());
        assert_eq!(voice(&editor), narration);
        assert!(editor.redo());
        assert_eq!(voice(&editor), narration);
    }
}

#[cfg(test)]
mod caption_preset_settings_tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn can_apply_before_captions_and_roundtrip_through_undo_redo() {
        let mut editor = crate::Editor::new();
        let settings = json!({
            "version": 1, "presetId": "cuda-caption.preset-2-yellow-bar",
            "generateOnGpu": true, "sourceClipId": null,
            "style": {"fontFamily": "Inter", "fontWeight": 900,
                      "color": "#000000", "background": "#ffff00"},
            "position": {"anchor": "bottom-center", "offsetX": 0.0,
                         "offsetY": 0.35, "safeMargin": 0.08}
        });
        editor.apply(Command::SetCaptionPresetSettings { settings: settings.clone() }).unwrap();
        assert!(editor.project().active().clips.is_empty());
        assert_eq!(editor.project().extra.get("captionPreset"), Some(&settings));
        assert!(editor.undo());
        assert!(!editor.project().extra.contains_key("captionPreset"));
        assert!(editor.redo());
        let doc = crate::to_document(&crate::DocumentSettings {
            name: "Caption metadata".into(), width: 1920, height: 1080,
            rate_num: 30, rate_den: 1,
        }, editor.project());
        let reopened = crate::Editor::from_document(&doc).unwrap();
        assert_eq!(reopened.project().extra.get("captionPreset"), Some(&settings));
        assert!(reopened.project().active().clips.is_empty());
    }

    #[test]
    fn native_gpu_caption_requests_preserve_worker_style_and_reopen() {
        let mut project = crate::model::Project::new();
        project.active_mut().clips.push(Arc::new(Clip::blank(
            "voice1", "T1", ClipKind::Audio, "Narration", 0.0, 4.0,
        )));
        let mut mint = IdMint::default();
        mint.adopt_project(&project);
        let request = json!({
            "version": 1, "sourceClipId": "voice1",
            "model": "small", "wordTimestamps": true,
            "renderAfter": true, "preset": "cuda-caption.preset-2-yellow-bar",
            "style": {"fontFamily": "Inter", "fontWeight": 900,
                      "color": "#000000", "background": "#ffff00",
                      "backgroundRadius": 0.012, "maxWidth": 0.88},
            "position": {"x": 0.5, "y": 0.85}
        });
        super::apply(&mut project, &mut mint,
            Command::SetGpuCaptionRequest { request: Some(request.clone()) }).unwrap();
        assert_eq!(project.extra.get("gpuCaptionRequest"), Some(&request));
        assert_eq!(project.active().clips.iter()
            .filter(|clip| clip.kind == ClipKind::Text).count(), 0);
        let document = crate::to_document(&crate::DocumentSettings {
            name: "Native caption request".into(), width: 1920, height: 1080,
            rate_num: 30, rate_den: 1,
        }, &project);
        let reopened = crate::Editor::from_document(&document).unwrap();
        assert_eq!(reopened.project().extra.get("gpuCaptionRequest"), Some(&request));
        let bad = json!({ "version": 1, "sourceClipId": "voice1",
            "model": "small", "wordTimestamps": true, "preset": "cuda-caption.fake" });
        assert!(super::apply(&mut project, &mut mint,
            Command::SetGpuCaptionRequest { request: Some(bad) }).is_err());
        assert_eq!(project.extra.get("gpuCaptionRequest"), Some(&request));
    }

    #[test]
    fn sample_words_are_refused_in_project_settings() {
        let mut project = crate::model::Project::new();
        let settings = json!({
            "version": 1, "presetId": "cuda-caption.preset-2-yellow-bar",
            "generateOnGpu": true, "style": {"content": "Sample caption preview"},
            "position": {"anchor": "bottom-center", "offsetY": 0.35}
        });
        assert!(super::apply(&mut project, &mut IdMint::default(),
            Command::SetCaptionPresetSettings { settings }).is_err());
        assert!(!project.extra.contains_key("captionPreset"));
    }
}
