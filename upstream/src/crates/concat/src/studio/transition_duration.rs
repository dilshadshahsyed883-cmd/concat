//! Plan the Transition inspector's duration-only bulk edit, without changing media.
use std::collections::{HashMap, HashSet};

use concat_project::commands::ClipPatch;
use concat_project::model::{Clip, ClipKind, Project, Timeline, ranges};
use concat_project::Command;

#[derive(Default)]
pub struct DurationPlan {
    pub commands: Vec<Command>,
    pub changed: usize,
    pub skipped: usize,
    pub unchanged: usize,
}

impl DurationPlan {
    pub fn summary(&self) -> String {
        format!(
            "Apply Duration to All: {} changed, {} skipped, {} already matched. \
             Skipped cuts are locked, unsupported, lack adjacent pictures, or cannot fit this duration without overlapping another transition. Shorten the duration or unlock the tracks.",
            self.changed, self.skipped, self.unchanged
        )
    }
}

/// Round once at each timeline's rational frame rate. The model's 0.1-second
/// minimum must also survive Save/Reopen; the worker requires at least two frames.
fn requested_duration(timeline: &Timeline, seconds: f64) -> Option<f64> {
    if timeline.video.rate_num <= 0 || timeline.video.rate_den <= 0 {
        return None;
    }
    let fps = timeline.video.rate_num as f64 / timeline.video.rate_den as f64;
    let minimum = (ranges::MIN_TRANSITION * fps - 1e-9).ceil().max(2.0);
    Some((seconds * fps).round().max(minimum) / fps)
}

fn pair_is_valid(previous: &Clip, incoming: &Clip, frame: f64, duration: f64) -> bool {
    matches!(previous.kind, ClipKind::Image | ClipKind::Video)
        && matches!(incoming.kind, ClipKind::Image | ClipKind::Video)
        && !previous.extra.contains_key("canvasOwner")
        && !incoming.extra.contains_key("canvasOwner")
        && (previous.start + previous.duration - incoming.start).abs() < frame / 2.0
        && duration <= previous.duration.min(incoming.duration) + 1e-9
}

/// Native CUDA packages center on the cut, limited by a video's source head.
/// Legacy worker transitions run forward from the cut. No source trim is changed;
/// the existing renderer's last-frame hold handles a missing outgoing tail.
fn span(incoming: &Clip, duration: f64, fps: f64, native: bool) -> (i64, i64) {
    let head = if native {
        if incoming.kind == ClipKind::Video {
            (duration / 2.0).min(incoming.source_start / incoming.speed.max(ranges::MIN_SPEED))
        } else {
            duration / 2.0
        }
    } else {
        0.0
    };
    let head = if head < 0.5 / fps { 0.0 } else { head };
    let start = ((incoming.start - head) * fps).round() as i64;
    (start, start + (duration * fps).round() as i64)
}

struct Cut<'a> {
    clip: &'a Clip,
    wanted: f64,
    candidate: bool,
    native: bool,
    before: (i64, i64),
    after: (i64, i64),
}

fn overlaps(a: (i64, i64), b: (i64, i64)) -> bool {
    a.0 < b.1 && b.0 < a.1
}

/// Only existing transitions are patched. A cut that cannot take the requested
/// frame count is skipped, rather than silently receiving a shorter duration.
/// Only the active timeline is visited; other timelines must not be edited.
pub fn plan(
    project: &Project,
    seconds: f64,
    locked: impl Fn(&str) -> bool,
    supported: impl Fn(&str) -> bool,
) -> Result<DurationPlan, &'static str> {
    if !seconds.is_finite() || !(ranges::MIN_TRANSITION..=14_400.0).contains(&seconds) {
        return Err("Enter a finite transition duration from 0.10 to 14,400 seconds.");
    }
    let mut result = DurationPlan::default();
    for timeline in &project.timelines {
        if timeline.id != project.active_timeline_id { continue; }
        let Some(wanted) = requested_duration(timeline, seconds) else {
            result.skipped += timeline.clips.iter().filter(|c| c.transition_in.is_some()).count();
            continue;
        };
        let fps = timeline.video.rate_num as f64 / timeline.video.rate_den as f64;
        // Sorting once avoids an O(n²) adjacency search on large timelines.
        let mut lanes: HashMap<&str, Vec<&Clip>> = HashMap::new();
        for clip in &timeline.clips {
            lanes.entry(&clip.track_id).or_default().push(clip);
        }
        let mut cuts = Vec::new();
        for clips in lanes.values_mut() {
            clips.sort_by(|a, b| a.start.total_cmp(&b.start).then_with(|| a.id.cmp(&b.id)));
            for (index, clip) in clips.iter().enumerate() {
                let Some(existing) = &clip.transition_in else { continue };
                let native = existing.id.starts_with("concat.");
                let candidate = !locked(&clip.track_id)
                    && timeline.tracks.iter().any(|track| track.id == clip.track_id && track.visible)
                    && supported(&existing.id)
                    && index.checked_sub(1).is_some_and(|previous| {
                        pair_is_valid(clips[previous], clip, 1.0 / fps, wanted)
                    });
                cuts.push(Cut {
                    clip,
                    wanted,
                    candidate,
                    native,
                    before: span(clip, existing.duration, fps, native),
                    after: span(clip, wanted, fps, native),
                });
            }
        }
        // A conservative interval sweep protects retained/skipped cuts too. A
        // proposed span must fit alongside both a neighbour's old and new span,
        // so a later skip never invalidates an earlier accepted edit.
        let mut intervals: Vec<(i64, i64, usize)> = Vec::new();
        for (index, cut) in cuts.iter().enumerate() {
            intervals.push((cut.before.0, cut.before.1, index));
            if cut.candidate {
                intervals.push((cut.after.0, cut.after.1, index));
            }
        }
        intervals.sort_unstable();
        let mut conflicts = HashSet::new();
        for (position, &(start, end, index)) in intervals.iter().enumerate() {
            for &(next_start, next_end, other) in &intervals[position + 1..] {
                if next_start >= end { break; }
                if index == other || (!cuts[index].native && !cuts[other].native) { continue; }
                // Native packages cannot overlap each other in the worker;
                // legacy/native spans are guarded on their shared picture lane.
                if !cuts[index].native || !cuts[other].native {
                    if cuts[index].clip.track_id != cuts[other].clip.track_id { continue; }
                }
                if cuts[index].candidate && overlaps(cuts[index].after, (next_start, next_end)) {
                    conflicts.insert(index);
                }
                if cuts[other].candidate && overlaps(cuts[other].after, (start, end)) {
                    conflicts.insert(other);
                }
            }
        }
        let conflict_ids: HashSet<String> = conflicts.iter()
            .map(|&index| cuts[index].clip.id.clone()).collect();
        let mut commands = Vec::new();
        // Stable command order also makes equal-start cuts deterministic.
        cuts.sort_by(|a, b| a.clip.id.cmp(&b.clip.id));
        for cut in &cuts {
            let existing = cut.clip.transition_in.as_ref().expect("existing transition");
            if !cut.candidate || conflict_ids.contains(&cut.clip.id) {
                result.skipped += 1;
            } else if (existing.duration - cut.wanted).abs() < 1e-9 {
                result.unchanged += 1;
            } else {
                let mut transition = existing.clone();
                transition.duration = cut.wanted;
                commands.push(Command::UpdateClip {
                    clip_id: cut.clip.id.clone(),
                    patch: ClipPatch { transition_in: Some(Some(transition)), ..Default::default() },
                });
                result.changed += 1;
            }
        }
        if !commands.is_empty() {
            result.commands.extend(commands);
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use concat_project::{Editor, DocumentSettings, from_document, to_document};
    use serde_json::{Value, json};
    use std::sync::Arc;

    fn fixture(count: usize) -> Project {
        let mut clips = vec![json!({"id":"voice", "kind":"audio", "trackId":"audio",
            "mediaId":"voice", "start":0., "duration":(count + 1) as f64 * 2.,
            "sourceStart":0.25, "volume":0.8}),
            json!({"id":"caption", "kind":"text", "trackId":"captions", "mediaId":"",
                "start":0., "duration":(count + 1) as f64 * 2.,
                "text":{"content":"Keep this caption", "fontFamily":"Hanken Grotesk", "fontWeight":600,"align":"center"}})];
        for index in 0..=count {
            let mut clip = json!({"id":format!("c{index}"), "kind":"image", "trackId":"video",
                "mediaId":"photo", "name":format!("Picture {index}"), "start":index as f64 * 2.,
                "duration":2., "sourceStart":0.2, "scale":1.2, "offsetX":0.1});
            if index > 0 {
                let id = ["concat.fade-black", "concat.clock-wipe", "concat.paper-rip-vertical"][(index - 1) % 3];
                let duration = [1.0, 0.5, 0.75][(index - 1) % 3];
                let intensity = [0.5, 0.6, 0.4][(index - 1) % 3];
                clip["transitionIn"] = json!({"id":id, "duration":duration, "intensity":intensity});
                if id == "concat.paper-rip-vertical" {
                    clip["transitionIn"]["params"] = json!({"roughness":0.6,"seed":42});
                }
            }
            clips.push(clip);
        }
        let value = json!({"version":1, "activeTimelineId":"main", "media":[
            {"id":"photo", "kind":"image", "path":"photo.jpg"},
            {"id":"voice", "kind":"audio", "path":"voice.wav", "duration":10000.}],
            "timelines":[{"id":"main", "video":{"width":1920,"height":1080,"rateNum":30,"rateDen":1},
                "tracks":[{"id":"video"},{"id":"audio"},{"id":"captions"}],"clips":clips}]});
        from_document(&value).expect("fixture is native document")
    }

    fn settings() -> DocumentSettings {
        DocumentSettings {name:"Transition duration".into(),width:1920,height:1080,rate_num:30,rate_den:1}
    }

    fn document(project: &Project) -> Value { to_document(&settings(), project) }

    fn run(project: &Project, seconds: f64) -> (DurationPlan, Editor) {
        let mut plan = plan(project, seconds, |_| false, |_| true).unwrap();
        let mut editor = Editor::from_document(&document(project)).unwrap();
        let commands = std::mem::take(&mut plan.commands);
        if !commands.is_empty() { editor.apply(Command::Batch {commands}).unwrap(); }
        (plan, editor)
    }

    #[test]
    fn mixed_duration_edit_only_changes_duration_and_is_one_undo_redo() {
        let project = fixture(3);
        let before = document(&project);
        let (plan, mut editor) = run(&project, 0.6);
        assert_eq!((plan.changed,plan.skipped,plan.unchanged),(3,0,0));
        let after = document(editor.project());
        let mut expected = before.clone();
        for timeline in expected["timelines"].as_array_mut().unwrap() {
            for clip in timeline["clips"].as_array_mut().unwrap() {
                if !clip["transitionIn"].is_null() { clip["transitionIn"]["duration"] = json!(0.6); }
            }
        }
        // The writer's legacy active-timeline mirror must carry the same values.
        expected["clips"] = expected["timelines"][0]["clips"].clone();
        assert_eq!(after, expected, "type, strength, params, source trims, narration, timing and IDs survive");
        assert!(editor.undo());
        assert_eq!(document(editor.project()), before);
        assert!(!editor.undo(), "bulk operation created exactly one history entry");
        assert!(editor.redo());
        assert_eq!(document(editor.project()), after);
        let reopened = Editor::from_document(&after).unwrap();
        assert_eq!(document(reopened.project()), after, "save/reopen cannot retime a cut");
    }

    #[test]
    fn handles_locks_unsupported_orphan_and_existing_matches_are_reported() {
        let mut project = fixture(6);
        let timeline = project.active_mut();
        timeline.clip_mut("c1").unwrap().transition_in.as_mut().unwrap().duration=0.6;
        timeline.clip_mut("c2").unwrap().duration=0.2;
        timeline.clip_mut("c3").unwrap().start=6.5; // orphan cut
        timeline.clip_mut("c4").unwrap().transition_in.as_mut().unwrap().id="unsupported".into();
        timeline.tracks.push(concat_project::model::Track {id:"locked".into(),..Default::default()});
        timeline.clip_mut("c5").unwrap().track_id="locked".into();
        let result = plan(&project,0.6,|track|track=="locked",|id|id!="unsupported").unwrap();
        assert_eq!((result.changed,result.skipped,result.unchanged),(0,5,1));
        assert!(result.commands.is_empty());
        assert!(result.summary().contains("0 changed, 5 skipped"));
    }

    #[test]
    fn zero_and_invalid_requests_never_add_transitions_or_history() {
        for project in [Project::new(),fixture(0)] {
            let result=plan(&project,0.6,|_|false,|_|true).unwrap();
            assert_eq!((result.changed,result.skipped,result.unchanged),(0,0,0));
            assert!(result.commands.is_empty());
        }
        for bad in [0.,-1.,f64::NAN,f64::INFINITY,14401.] {
            assert!(plan(&fixture(1),bad,|_|false,|_|true).is_err());
        }
    }

    #[test]
    fn rational_rates_quantize_once_and_never_round_past_a_short_clip() {
        for (num,den,seconds,expected_frames) in [(30,1,0.614,18.),(30,1,0.617,19.),(24,1,0.1,3.),(30000,1001,0.6,18.)] {
            let mut project=fixture(1);
            project.active_mut().video.rate_num=num;
            project.active_mut().video.rate_den=den;
            let (result,editor)=run(&project,seconds);
            assert_eq!(result.changed,1);
            let value=editor.project().active().clip("c1").unwrap().transition_in.as_ref().unwrap().duration;
            assert!((value*num as f64/den as f64-expected_frames).abs()<1e-9);
            assert_eq!(document(Editor::from_document(&document(editor.project())).unwrap().project()),document(editor.project()));
        }
        let mut project=fixture(1);
        project.active_mut().clip_mut("c1").unwrap().duration=0.61;
        let result=plan(&project,0.617,|_|false,|_|true).unwrap();
        assert_eq!((result.changed,result.skipped),(0,1));
    }

    #[test]
    fn project_wide_batch_restores_active_timeline_and_scales_to_thousands() {
        let mut project=fixture(1500);
        let mut other=project.active().clone();
        other.id="other".into();
        for clip in &mut other.clips { Arc::make_mut(clip).id.push_str("-other"); }
        project.timelines.push(Arc::new(other));
        let (result,mut editor)=run(&project,0.6);
        assert_eq!((result.changed,result.skipped),(3000,0));
        assert_eq!(editor.project().active_timeline_id,"main");
        assert!(editor.undo());
        assert_eq!(document(editor.project()),document(&project));
    }

    #[test]
    fn proposed_overlapping_native_spans_are_skipped_without_touching_clips() {
        let mut project=fixture(2);
        let timeline=project.active_mut();
        for index in 0..=2 {
            let clip=timeline.clip_mut(&format!("c{index}")).unwrap();
            clip.start=index as f64*0.6;
            clip.duration=0.6;
            if index==1 {clip.kind=ClipKind::Video;clip.source_start=0.;}
            if let Some(transition)=&mut clip.transition_in {transition.duration=0.1;}
        }
        let result=plan(&project,0.6,|_|false,|_|true).unwrap();
        assert_eq!((result.changed,result.skipped),(0,2));
        assert!(result.commands.is_empty());
    }

    #[test]
    fn write_real_duration_edit_for_worker_zip_regression() {
        let (_,editor)=run(&fixture(3),0.6);
        if let Ok(directory)=std::env::var("CONCAT_TRANSITION_FIXTURE_DIR") {
            let path=std::path::Path::new(&directory);
            std::fs::create_dir_all(path).unwrap();
            std::fs::write(path.join("bulk-transition-duration.json"),serde_json::to_vec_pretty(&document(editor.project())).unwrap()).unwrap();
        }
    }
}
