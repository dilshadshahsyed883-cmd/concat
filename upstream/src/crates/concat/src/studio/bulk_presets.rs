// SPDX-License-Identifier: AGPL-3.0-or-later

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::hash::{BuildHasher, RandomState};
use std::path::Path;

use concat_project::model::{ClipKey, ClipKind, VideoSettings};

use super::{
    Catalogue, Clip, ClipPatch, Command, MotionPreset, PackageKind, Studio, Transition,
    gpu_catalogue_has, key_commands, t, tf,
};

const MOTION_PRESETS: [MotionPreset; 3] = [
    MotionPreset::PushIn,
    MotionPreset::PanRight,
    MotionPreset::FadeIn,
];

#[derive(Clone, Copy, PartialEq)]
struct GeneratedMotion {
    preset: MotionPreset,
    keys: [ClipKey; 2],
}

/// Keep the signatures of preset keys this editing session produced. The
/// project stores ordinary editable keys and needs no new fields.
#[derive(Default)]
pub(super) struct BulkMotion(HashMap<(String, String), Vec<GeneratedMotion>>);

impl BulkMotion {
    pub(super) fn clear(&mut self) {
        self.0.clear();
    }

    fn remember(&mut self, target: (String, String), motion: GeneratedMotion) {
        let signatures = self.0.entry(target).or_default();
        signatures.retain(|old| old.preset != motion.preset);
        signatures.push(motion);
    }
}

/// Draw independently per target. If the complete assignment would repeat
/// and any target can vary, pick a random one of those targets and vary it.
fn random_assignment(
    options: &[(usize, Option<usize>)],
    mut draw: impl FnMut(usize) -> usize,
) -> Vec<usize> {
    let mut chosen: Vec<_> = options.iter().map(|&(count, _)| draw(count)).collect();
    if chosen
        .iter()
        .zip(options)
        .all(|(&next, &(_, old))| Some(next) == old)
    {
        let alternatives: Vec<_> = options
            .iter()
            .enumerate()
            .filter_map(|(index, &(count, _))| (count > 1).then_some(index))
            .collect();
        if !alternatives.is_empty() {
            let target = alternatives[draw(alternatives.len())];
            let old = chosen[target];
            let alternative = draw(options[target].0 - 1);
            chosen[target] = alternative + usize::from(alternative >= old);
        }
    }
    chosen
}

fn fresh_draw() -> impl FnMut(usize) -> usize {
    let seed = RandomState::new();
    let mut index = 0_u64;
    move |count| {
        index += 1;
        (seed.hash_one(index) % count as u64) as usize
    }
}

fn picture(clip: &Clip) -> bool {
    matches!(clip.kind, ClipKind::Image | ClipKind::Video)
}

const FPS: f64 = 30.0;

#[derive(Clone, Copy)]
struct CutSpan {
    start: i64,
    end: i64,
}

impl CutSpan {
    fn overlaps(self, other: Self) -> bool {
        self.start < other.end && other.start < self.end
    }
}

fn frame(seconds: f64) -> i64 {
    (seconds * FPS).round_ties_even() as i64
}

/// The worker centers a native fade when the incoming clip has source
/// frames before the cut. A legacy transition starts on the cut itself.
fn transition_span(previous: &Clip, incoming: &Clip, duration: f64, native: bool) -> CutSpan {
    let cut = frame(incoming.start);
    let room = (frame(previous.start + previous.duration) - frame(previous.start))
        .min(frame(incoming.start + incoming.duration) - cut);
    if !native {
        return CutSpan {
            start: cut,
            end: cut + frame(duration),
        };
    }
    let length = duration.min(room as f64 / FPS);
    let mut head = length / 2.0;
    if incoming.kind == ClipKind::Video {
        head = head.min(incoming.source_start / incoming.speed);
    }
    if head < 0.5 / FPS {
        head = 0.0;
    }
    let start = (cut as f64 - head * FPS).round() as i64;
    CutSpan {
        start,
        end: start + (length * FPS).round() as i64,
    }
}

/// Match the worker's 30fps lane rounding and the native transition's
/// requirement to fit both sides of the seam.
fn transition_length(previous: &Clip, incoming: &Clip) -> Option<(f64, CutSpan)> {
    let previous_end = previous.start + previous.duration;
    if (previous_end - incoming.start).abs() >= 0.5 / FPS
        || frame(previous_end) != frame(incoming.start)
    {
        return None;
    }
    let requested = incoming
        .transition_in
        .as_ref()
        .map_or(0.5, |cut| cut.duration);
    let length = requested
        .max(2.0 / FPS)
        .min(previous.duration)
        .min(incoming.duration);
    let span = transition_span(previous, incoming, length, true);
    (span.end - span.start >= 2).then_some((length, span))
}

fn motion_choices(
    clip: &Clip,
    frame: f64,
    generated: &[GeneratedMotion],
) -> Option<(Vec<GeneratedMotion>, Option<usize>)> {
    let previous = generated
        .iter()
        .find(|motion| clip.keys.as_slice() == motion.keys);
    let base = if clip.keys.is_empty() {
        Cow::Borrowed(clip)
    } else if previous.is_some() {
        let mut clean = clip.clone();
        clean.keys.clear();
        Cow::Owned(clean)
    } else {
        // Keys from the inspector or a project opened from disk are authored.
        return None;
    };
    let choices: Vec<_> = MOTION_PRESETS
        .into_iter()
        .filter_map(|preset| {
            let (keys, _) = preset.keys(&base, frame).ok()?;
            Some(GeneratedMotion { preset, keys })
        })
        .collect();
    let current =
        previous.and_then(|previous| choices.iter().position(|choice| choice == previous));
    (!choices.is_empty()).then_some((choices, current))
}

impl Studio {
    fn gpu_picture_media(&self) -> HashSet<&str> {
        self.project()
            .media
            .iter()
            .filter(|item| {
                !item.placeholder
                    && (item.kind != concat_project::model::MediaKind::Image
                        || Path::new(&item.path)
                            .extension()
                            .and_then(|ext| ext.to_str())
                            .is_some_and(|ext| {
                                ext.eq_ignore_ascii_case("jpg") || ext.eq_ignore_ascii_case("jpeg")
                            }))
            })
            .map(|item| item.id.as_str())
            .collect()
    }

    fn gpu_bulk_ready(&mut self) -> bool {
        self.flush_commit();
        if self.timeline().video != VideoSettings::default() {
            self.notify(&t("studio.randomGpuFormat"), true);
            return false;
        }
        true
    }

    /// Apply native GPU-supported transitions to every eligible cut in the
    /// active timeline, with a single command for undo.
    pub fn apply_random_gpu_transitions(&mut self) {
        if !self.gpu_bulk_ready() {
            return;
        }
        let presets: Vec<_> = Catalogue::builtin()
            .of_kind(PackageKind::Transition)
            .map(|package| package.id())
            .filter(|id| gpu_catalogue_has("transitions", id))
            .collect();
        if presets.is_empty() {
            return;
        }
        let media = self.gpu_picture_media();
        let mut lanes: HashMap<_, Vec<_>> = self
            .timeline()
            .tracks
            .iter()
            .filter(|track| track.visible)
            .map(|track| (track.id.as_str(), Vec::new()))
            .collect();
        for clip in &self.timeline().clips {
            if (picture(clip) || clip.kind == ClipKind::Layer)
                && let Some(lane) = lanes.get_mut(clip.track_id.as_str())
            {
                lane.push(clip.as_ref());
            }
        }
        let mut plans = Vec::new();
        let mut occupied = Vec::new();
        for lane in lanes.values_mut() {
            lane.sort_by(|a, b| a.start.total_cmp(&b.start));
            for pair in lane.windows(2) {
                let [previous, incoming] = [pair[0], pair[1]];
                if !picture(previous)
                    || !picture(incoming)
                    || !media.contains(previous.media_id.as_str())
                    || !media.contains(incoming.media_id.as_str())
                {
                    continue;
                }
                let existing = incoming.transition_in.as_ref();
                let old = existing.and_then(|cut| presets.iter().position(|id| *id == cut.id));
                if let Some(cut) = existing {
                    occupied.push((
                        &incoming.id,
                        transition_span(previous, incoming, cut.duration, old.is_some()),
                    ));
                }
                if !self.locked(&incoming.track_id)
                    && let Some((duration, span)) = transition_length(previous, incoming)
                {
                    plans.push((incoming, duration, old, span));
                }
            }
        }
        plans.sort_by(|(a, _, _, a_span), (b, _, _, b_span)| {
            (a_span.start, &a.id).cmp(&(b_span.start, &b.id))
        });
        let mut previous_span: Option<CutSpan> = None;
        plans.retain(|(clip, _, _, span)| {
            if previous_span.is_some_and(|previous| previous.overlaps(*span))
                || occupied
                    .iter()
                    .any(|(id, existing)| *id != &clip.id && existing.overlaps(*span))
            {
                return false;
            }
            previous_span = Some(*span);
            true
        });
        if plans.is_empty() {
            self.notify(&t("studio.randomNoGpuCuts"), true);
            return;
        }
        let options: Vec<_> = plans.iter().map(|plan| (presets.len(), plan.2)).collect();
        let commands: Vec<_> = plans
            .into_iter()
            .zip(random_assignment(&options, fresh_draw()))
            .filter_map(|((clip, duration, old, _), chosen)| {
                (old != Some(chosen)).then(|| Command::UpdateClip {
                    clip_id: clip.id.clone(),
                    patch: ClipPatch {
                        transition_in: Some(Some(Transition {
                            id: presets[chosen].to_owned(),
                            duration,
                        })),
                        ..ClipPatch::default()
                    },
                })
            })
            .collect();
        if commands.is_empty() {
            self.notify(&t("studio.randomGpuCutsUnchanged"), false);
            return;
        }
        let count = commands.len();
        let revision = self.revision;
        self.apply(Command::Batch { commands });
        if self.revision != revision {
            self.notify(
                &tf("studio.randomGpuCutsApplied", &[&count.to_string()]),
                false,
            );
        }
    }

    /// Reroll bulk motion without replacing manually authored keyframes.
    pub fn apply_random_gpu_motion(&mut self) {
        if !self.gpu_bulk_ready() {
            return;
        }
        let timeline = self.timeline();
        let frame = self.frame_seconds();
        let media = self.gpu_picture_media();
        let tracks: HashSet<_> = timeline
            .tracks
            .iter()
            .filter(|track| track.visible && !self.locked(&track.id))
            .map(|track| track.id.as_str())
            .collect();
        let plans: Vec<_> = timeline
            .clips
            .iter()
            .filter(|clip| {
                picture(clip)
                    && tracks.contains(clip.track_id.as_str())
                    && media.contains(clip.media_id.as_str())
            })
            .filter_map(|clip| {
                let target = (timeline.id.clone(), clip.id.clone());
                let held = self
                    .bulk_motion
                    .0
                    .get(&target)
                    .map_or(&[][..], Vec::as_slice);
                let (choices, old) = motion_choices(clip, frame, held)?;
                Some((clip, target, choices, old))
            })
            .collect();
        if plans.is_empty() {
            self.notify(&t("studio.randomNoGpuMotion"), true);
            return;
        }
        let options: Vec<_> = plans
            .iter()
            .map(|(_, _, choices, old)| (choices.len(), *old))
            .collect();
        let mut updates = Vec::new();
        let mut commands = Vec::new();
        for ((clip, target, choices, old), chosen) in plans
            .into_iter()
            .zip(random_assignment(&options, fresh_draw()))
        {
            if old == Some(chosen) {
                continue;
            }
            let generated = choices[chosen];
            let mut result = clip.as_ref().clone();
            result.keys = generated.keys.to_vec();
            commands.extend(key_commands(&clip.id, clip, &result));
            updates.push((target, generated));
        }
        if commands.is_empty() {
            self.notify(&t("studio.randomGpuMotionUnchanged"), false);
            return;
        }
        let revision = self.revision;
        self.apply(Command::Batch { commands });
        if self.revision != revision {
            let count = updates.len();
            for (target, motion) in updates {
                self.bulk_motion.remember(target, motion);
            }
            self.notify(
                &tf("studio.randomGpuMotionApplied", &[&count.to_string()]),
                false,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_full_repeat_is_changed_where_another_choice_exists() {
        assert_eq!(
            random_assignment(&[(1, Some(0)), (3, Some(0))], |_| 0),
            vec![0, 1]
        );
        assert_eq!(random_assignment(&[(1, Some(0))], |_| 0), vec![0]);
        assert_eq!(
            random_assignment(&[(3, Some(2)), (3, Some(0))], |_| 0),
            vec![0, 0]
        );
    }

    #[test]
    fn only_untouched_bulk_motion_can_be_rerolled() {
        let mut clip = Clip::blank("c1", "t1", ClipKind::Video, "picture", 0.0, 2.0);
        let (choices, old) = motion_choices(&clip, 1.0 / 30.0, &[]).unwrap();
        assert_eq!(choices.len(), 3);
        assert_eq!(old, None);
        let generated = choices[0];
        clip.keys = generated.keys.to_vec();
        assert!(motion_choices(&clip, 1.0 / 30.0, &[]).is_none());
        assert_eq!(
            motion_choices(&clip, 1.0 / 30.0, &[generated]).unwrap().1,
            Some(0)
        );
        clip.keys[1].value += 0.02;
        assert!(motion_choices(&clip, 1.0 / 30.0, &[generated]).is_none());
    }

    #[test]
    fn only_frame_aligned_cuts_with_room_on_both_sides_are_eligible() {
        let previous = Clip::blank("c1", "t1", ClipKind::Video, "previous", 0.0, 1.0);
        let mut incoming = Clip::blank("c2", "t1", ClipKind::Image, "incoming", 1.0, 1.0);
        let (length, span) = transition_length(&previous, &incoming).unwrap();
        assert_eq!(length, 0.5);
        assert_eq!((span.start, span.end), (23, 38));
        incoming.kind = ClipKind::Video;
        let (_, span) = transition_length(&previous, &incoming).unwrap();
        assert_eq!((span.start, span.end), (30, 45));
        incoming.start = 1.1;
        assert!(transition_length(&previous, &incoming).is_none());
        incoming.start = 1.0;
        incoming.duration = 1.0 / 30.0;
        assert!(transition_length(&previous, &incoming).is_none());
    }
}
