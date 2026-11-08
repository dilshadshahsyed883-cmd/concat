// SPDX-License-Identifier: AGPL-3.0-or-later

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::hash::{BuildHasher, RandomState};
use std::path::Path;

use concat_project::model::{ClipKey, ClipKind, KeyProperty, VideoSettings};
use concat_project::physics_motion::{PhysicsControls, PhysicsPreset};

use super::{
    AppliedFilter, Catalogue, Clip, ClipPatch, Command, MotionPreset, PackageKind, Studio, Transition,
    gpu_catalogue_has, key_commands, t, tf,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum MotionKind {
    Basic(MotionPreset),
    Physics(PhysicsPreset),
}

const MOTION_PRESETS: [MotionKind; 9] = [
    MotionKind::Basic(MotionPreset::PushIn),
    MotionKind::Basic(MotionPreset::PanRight),
    MotionKind::Basic(MotionPreset::FadeIn),
    MotionKind::Physics(PhysicsPreset::SpringZoom),
    MotionKind::Physics(PhysicsPreset::Pendulum),
    MotionKind::Physics(PhysicsPreset::GravityBounce),
    MotionKind::Physics(PhysicsPreset::ElasticPop),
    MotionKind::Physics(PhysicsPreset::InertiaGlide),
    MotionKind::Physics(PhysicsPreset::DampedShake),
];

#[derive(Clone, PartialEq)]
struct GeneratedMotion {
    preset: MotionKind,
    keys: Vec<ClipKey>,
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


#[derive(Clone)]
struct GeneratedEffect {
    index: usize,
    entry: AppliedFilter,
}

/// This is only session state, not a project-format change. If an authored
/// edit changes the entry, it stops being owned by the bulk operation.
#[derive(Default)]
pub(super) struct BulkEffects(HashMap<(String, String), GeneratedEffect>);

impl BulkEffects {
    pub(super) fn clear(&mut self) {
        self.0.clear();
    }
}

#[derive(Default)]
pub(super) struct BulkTransitions(HashMap<(String, String), Transition>);

impl BulkTransitions {
    pub(super) fn clear(&mut self) {
        self.0.clear();
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

/// Stable, clip-specific physics controls: the saved keyframes remain the
/// source of truth, while each media item receives different motion energy.
fn motion_controls(clip_id: &str) -> PhysicsControls {
    let hash = clip_id.bytes().fold(0xcbf29ce484222325_u64, |value, byte| {
        (value ^ u64::from(byte)).wrapping_mul(0x100000001b3)
    });
    let unit = |shift| (hash.rotate_left(shift) % 101) as f64 / 100.0;
    PhysicsControls {
        strength: 0.85 + 0.30 * unit(7),
        duration: 0.75 + 0.25 * unit(19),
        speed: 0.80 + 0.45 * unit(31),
        intensity: 0.70 + 0.60 * unit(43),
    }
}

fn chosen_media_type(clip: &Clip, images: bool, videos: bool) -> bool {
    match clip.kind {
        ClipKind::Image => images,
        ClipKind::Video => videos,
        _ => false,
    }
}

/// Return at most one transform edit per clip. A manually animated property
/// is skipped independently; applying scale never overwrites animated X/Y.
fn bulk_transform_command(
    clip: &Clip, mode: i32, scale: f64, x: f64, y: f64,
) -> Option<Command> {
    let scale = (mode == 0
        && !clip.is_keyed(KeyProperty::Scale)
        && (clip.scale - scale).abs() > 1e-9).then_some(scale);
    let offset_x = (mode == 1
        && !clip.is_keyed(KeyProperty::OffsetX)
        && (clip.offset_x - x).abs() > 1e-9).then_some(x);
    let offset_y = (mode == 1
        && !clip.is_keyed(KeyProperty::OffsetY)
        && (clip.offset_y - y).abs() > 1e-9).then_some(y);
    if scale.is_none() && offset_x.is_none() && offset_y.is_none() {
        return None;
    }
    Some(Command::SetClipTransform {
        clip_id: clip.id.clone(),
        scale,
        offset_x,
        offset_y,
        rotation: None,
        stretch_x: None,
        stretch_y: None,
    })
}

fn motion_choices(
    clip: &Clip,
    frame: f64,
    generated: &[GeneratedMotion],
    permitted: &[MotionKind],
) -> Option<(Vec<GeneratedMotion>, Option<usize>)> {
    let previous = generated
        .iter()
        .find(|motion| clip.keys.as_slice() == motion.keys.as_slice());
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
        .filter(|preset| permitted.contains(preset))
        .filter_map(|preset| {
            let (keys, _) = match preset {
                MotionKind::Basic(basic) => {
                    let (keys, preview) = basic.keys(&base, frame).ok()?;
                    (keys.to_vec(), preview)
                }
                // Vary physics per clip, with stable parameters so a reroll
                // recognizes exactly the keys that the previous bulk action owns.
                MotionKind::Physics(physics) => physics.keys_with(
                    &base, frame, motion_controls(&clip.id),
                ).ok()?,
            };
            Some(GeneratedMotion { preset, keys })
        })
        .collect();
    let current =
        previous.and_then(|previous| choices.iter().position(|choice| choice == previous));
    (!choices.is_empty()).then_some((choices, current))
}


fn gpu_effect_preset(id: &str) -> Option<AppliedFilter> {
    // Never advertise an effect unless both the catalogue and the worker
    // support it. Values are deliberately non-neutral and within the worker's
    // validated parameter bounds (Exposure +/-4 EV; Contrast +/-100).
    let mut entry = AppliedFilter::new(id);
    match id {
        "concat.exposure" => {
            entry.params.insert("stops".to_owned(), 0.5);
        }
        "concat.contrast" => {
            entry.params.insert("amount".to_owned(), 25.0);
        }
        _ => return None,
    }
    Some(entry)
}

/// Treat every other chain entry as manually authored, including a previously
/// generated entry whose index, parameters or enabled status were edited.
fn effect_choices(
    clip: &Clip,
    saved: Option<&GeneratedEffect>,
    presets: &[AppliedFilter],
) -> Option<(Vec<AppliedFilter>, Option<usize>, Option<usize>)> {
    let managed = saved.filter(|held| clip.video_effects.get(held.index) == Some(&held.entry));
    let managed_index = managed.map(|held| held.index);
    let choices: Vec<_> = presets
        .iter()
        .filter(|preset| {
            !clip.video_effects.iter().enumerate().any(|(index, applied)| {
                Some(index) != managed_index && applied.id == preset.id
            })
        })
        .cloned()
        .collect();
    let old = managed.and_then(|held| {
        choices.iter().position(|choice| choice.id == held.entry.id)
    });
    (!choices.is_empty()).then_some((choices, old, managed_index))
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
    pub fn apply_random_gpu_transitions(&mut self, selected_only: bool, favourites_only: bool) {
        if !self.gpu_bulk_ready() {
            return;
        }
        // Selection is evaluated on the incoming clip at each cut. This
        // supports one selected clip as well as multi-selected timeline clips.
        let selected: HashSet<String> = self.selection.iter().cloned().collect();
        if selected_only && selected.is_empty() {
            self.notify("Select one or more incoming timeline clips first.", true);
            return;
        }
        let presets: Vec<_> = Catalogue::builtin()
            .of_kind(PackageKind::Transition)
            .map(|package| package.id())
            .filter(|id| gpu_catalogue_has("transitions", id))
            .filter(|id| !favourites_only || self.prefs.favourites.iter().any(|star| star == id))
            .collect();
        if presets.is_empty() {
            if favourites_only {
                self.notify("Star at least one GPU-compatible transition first.", true);
            }
            return;
        }

        let media = self.gpu_picture_media();
        let timeline_id = self.timeline().id.clone();
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
                    || (selected_only && !selected.contains(&incoming.id))
                    || !media.contains(previous.media_id.as_str())
                    || !media.contains(incoming.media_id.as_str())
                {
                    continue;
                }
                let existing = incoming.transition_in.as_ref();

                let target = (timeline_id.clone(), incoming.id.clone());
                let old = existing
                    .filter(|cut| self.bulk_transitions.0.get(&target) == Some(*cut))
                    .and_then(|cut| presets.iter().position(|id| *id == cut.id));
                if let Some(cut) = existing {
                    let native = presets.iter().any(|id| *id == cut.id);
                    occupied.push((
                        &incoming.id,
                        transition_span(previous, incoming, cut.duration, native),
                    ));
                    // A manually placed transition is not owned by this button.
                    if old.is_none() {
                        continue;
                    }
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

        let mut generated = Vec::new();
        let commands: Vec<_> = plans
            .into_iter()
            .zip(random_assignment(&options, fresh_draw()))
            .filter_map(|((clip, duration, old, _), chosen)| {
                if old == Some(chosen) {
                    return None;
                }
                let transition = Transition {
                    id: presets[chosen].to_owned(),
                    duration,
                    intensity: None,
                };
                generated.push((
                    (timeline_id.clone(), clip.id.clone()),
                    transition.clone(),
                ));
                Some(Command::UpdateClip {
                    clip_id: clip.id.clone(),
                    patch: ClipPatch {
                        transition_in: Some(Some(transition)),
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
            for (target, transition) in generated {
                self.bulk_transitions.0.insert(target, transition);
            }
            self.notify(
                &tf("studio.randomGpuCutsApplied", &[&count.to_string()]),
                false,
            );
        }
    }


    /// Draw one compatible video effect per eligible picture clip. A second
    /// click replaces only the exact entry created by an earlier bulk click;
    /// manually added/edited effects stay where they are. One batch = one Undo.
    pub fn apply_random_gpu_effects(&mut self) {
        if !self.gpu_bulk_ready() {
            return;
        }
        let presets: Vec<_> = Catalogue::builtin()
            .of_kind(PackageKind::Effect)
            .map(|package| package.id())
            .filter(|id| gpu_catalogue_has("video_effects", id))
            .filter_map(gpu_effect_preset)
            .collect();
        if presets.is_empty() {
            return;
        }
        let timeline = self.timeline();
        let media = self.gpu_picture_media();
        let tracks: HashSet<_> = timeline
            .tracks
            .iter()
            .filter(|track| track.visible && !self.locked(&track.id))
            .map(|track| track.id.as_str())
            .collect();
        let mut plans: Vec<_> = timeline
            .clips
            .iter()
            .filter(|clip| {
                picture(clip)
                    && tracks.contains(clip.track_id.as_str())
                    && media.contains(clip.media_id.as_str())
            })
            .filter_map(|clip| {
                let target = (timeline.id.clone(), clip.id.clone());
                let saved = self.bulk_effects.0.get(&target);
                let (choices, old, managed_index) = effect_choices(clip, saved, &presets)?;
                Some((clip, target, choices, old, managed_index))
            })
            .collect();
        plans.sort_by(|(a, ..), (b, ..)| {
            a.start.total_cmp(&b.start).then_with(|| a.id.cmp(&b.id))
        });
        if plans.is_empty() {
            self.notify(&t("studio.randomNoGpuEffects"), true);
            return;
        }
        // Timeline ordering lets consecutive clips avoid the same favourite.
        plans.sort_by(|a, b| a.0.start.total_cmp(&b.0.start)
            .then_with(|| a.0.id.cmp(&b.0.id)));
        let options: Vec<_> = plans
            .iter()
            .map(|(_, _, choices, old, _)| (choices.len(), *old))
            .collect();
        let mut commands = Vec::new();
        let mut generated = Vec::new();
        for ((clip, target, choices, old, managed_index), chosen) in plans
            .into_iter()
            .zip(random_assignment(&options, fresh_draw()))
        {
            if old == Some(chosen) {
                continue;
            }
            let entry = choices[chosen].clone();
            let mut effects = clip.video_effects.clone();
            let index = if let Some(index) = managed_index {
                effects[index] = entry.clone();
                index
            } else {
                effects.push(entry.clone());
                effects.len() - 1
            };
            commands.push(Command::UpdateClip {
                clip_id: clip.id.clone(),
                patch: ClipPatch {
                    video_effects: Some(effects),
                    ..ClipPatch::default()
                },
            });
            generated.push((target, GeneratedEffect { index, entry }));
        }
        if commands.is_empty() {
            self.notify(&t("studio.randomGpuEffectsUnchanged"), false);
            return;
        }
        let revision = self.revision;
        self.apply(Command::Batch { commands });
        if self.revision != revision {
            let count = generated.len();
            for (target, effect) in generated {
                self.bulk_effects.0.insert(target, effect);
            }
            self.notify(
                &tf("studio.randomGpuEffectsApplied", &[&count.to_string()]),
                false,
            );
        }
    }

    /// Kept as compatibility callbacks for older Animations-tab clients.
    pub fn apply_random_gpu_motion(&mut self) {
        self.apply_bulk_gpu_motion(1, true, true, false);
    }

    pub fn apply_random_favourite_gpu_motion(&mut self) {
        self.apply_bulk_gpu_motion(2, true, true, false);
    }

    /// Modes: 0 = same preset to all, 1 = random compatible animations,
    /// 2 = random starred physics animations. Target types are independent.
    /// The command batch is deliberately one undo/redo unit.
    pub fn apply_bulk_gpu_motion(
        &mut self, mode: i32, images: bool, videos: bool, selected_only: bool,
    ) {
        if !images && !videos {
            self.notify("Choose Images, Video Clips, or both before bulk editing.", true);
            return;
        }
        if !(0..=2).contains(&mode) {
            self.notify("Unknown bulk animation action.", true);
            return;
        }
        let selected: HashSet<String> = self.selection.iter().cloned().collect();
        if selected_only && selected.is_empty() {
            self.notify("Select one or more timeline media clips first.", true);
            return;
        }
        if !self.gpu_bulk_ready() {
            return;
        }
        let favourites_only = mode == 2;
        let permitted: Vec<MotionKind> = MOTION_PRESETS
            .into_iter()
            .filter(|preset| !favourites_only || matches!(preset,
                MotionKind::Physics(physics) if self.prefs.physics_motion_favourites
                    .iter().any(|id| id == physics.id())))
            .collect();
        if permitted.is_empty() {
            self.notify("Star at least one physics animation first.", true);
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
        let mut plans: Vec<_> = timeline
            .clips
            .iter()
            .filter(|clip| {
                chosen_media_type(clip, images, videos)
                    && (!selected_only || selected.contains(&clip.id))
                    && tracks.contains(clip.track_id.as_str())
                    && media.contains(clip.media_id.as_str())
            })
            .filter_map(|clip| {
                let target = (timeline.id.clone(), clip.id.clone());
                let held = self.bulk_motion.0.get(&target).map_or(&[][..], Vec::as_slice);
                let (choices, old) = motion_choices(clip, frame, held, &permitted)?;
                Some((clip, target, choices, old))
            })
            .collect();
        // Never depend on HashMap/track traversal ordering when applying
        // adjacent favourites: the media sequence must define that order.
        plans.sort_by(|a, b| a.0.start.total_cmp(&b.0.start)
            .then_with(|| a.0.id.cmp(&b.0.id)));
        if plans.is_empty() {
            self.notify(&t("studio.randomNoGpuMotion"), true);
            return;
        }
        let options: Vec<_> = plans.iter()
            .map(|(_, _, choices, old)| (choices.len(), *old))
            .collect();
        let picks = if mode == 0 {
            vec![0; plans.len()]
        } else {
            random_assignment(&options, fresh_draw())
        };
        let mut updates = Vec::new();
        let mut commands = Vec::new();
        let mut last_preset = None;
        for ((clip, target, choices, old), mut chosen) in plans.into_iter().zip(picks) {
            if favourites_only && Some(choices[chosen].preset) == last_preset {
                if let Some(alternative) = choices.iter().position(|entry|
                    Some(entry.preset) != last_preset && Some(entry.preset) !=
                    old.and_then(|index| choices.get(index).map(|item| item.preset))) {
                    chosen = alternative;
                } else if let Some(alternative) = choices.iter().position(|entry|
                    Some(entry.preset) != last_preset) {
                    chosen = alternative;
                }
            }
            last_preset = Some(choices[chosen].preset);
            if old == Some(chosen) {
                continue;
            }
            let generated = choices[chosen].clone();
            let mut result = clip.as_ref().clone();
            result.keys = generated.keys.clone();
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
            self.notify(&tf("studio.randomGpuMotionApplied", &[&count.to_string()]), false);
        }
    }

    /// Bulk transform uses exactly the same native command and GPU units as
    /// the single-clip inspector: scale=1 is 100%, X/Y=1 is 100% of canvas.
    pub fn apply_bulk_gpu_transform(
        &mut self, mode: i32, images: bool, videos: bool,
        selected_only: bool, scale: f32, x: f32, y: f32,
    ) {
        if !images && !videos {
            self.notify("Choose Images, Video Clips, or both before bulk editing.", true);
            return;
        }
        if !matches!(mode, 0 | 1) {
            self.notify("Unknown bulk transform action.", true);
            return;
        }
        let (scale, x, y) = (f64::from(scale), f64::from(x), f64::from(y));
        if !scale.is_finite() || !x.is_finite() || !y.is_finite()
            || !(0.05..=8.0).contains(&scale)
            || !(-1.0..=1.0).contains(&x) || !(-1.0..=1.0).contains(&y) {
            self.notify("Bulk scale/position values are out of range.", true);
            return;
        }
        let selected: HashSet<String> = self.selection.iter().cloned().collect();
        if selected_only && selected.is_empty() {
            self.notify("Select one or more timeline media clips first.", true);
            return;
        }
        if !self.gpu_bulk_ready() {
            return;
        }
        let timeline = self.timeline();
        let media = self.gpu_picture_media();
        let tracks: HashSet<_> = timeline.tracks.iter()
            .filter(|track| track.visible && !self.locked(&track.id))
            .map(|track| track.id.as_str())
            .collect();
        let commands: Vec<_> = timeline.clips.iter()
            .filter(|clip| chosen_media_type(clip, images, videos)
                && (!selected_only || selected.contains(&clip.id))
                && tracks.contains(clip.track_id.as_str())
                && media.contains(clip.media_id.as_str()))
            .filter_map(|clip| bulk_transform_command(clip, mode, scale, x, y))
            .collect();
        if commands.is_empty() {
            self.notify("No eligible media to transform (manual keyframes are protected).", true);
            return;
        }
        let count = commands.len();
        let revision = self.revision;
        self.apply(Command::Batch { commands });
        if self.revision != revision {
            self.notify(&format!("Updated {count} media clips as one undo step."), false);
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
    fn video_effect_rerolls_preserve_authored_chain_and_do_not_stack() {
        let presets = vec![
            gpu_effect_preset("concat.exposure").unwrap(),
            gpu_effect_preset("concat.contrast").unwrap(),
        ];
        assert!(gpu_effect_preset("concat.saturation").is_none());
        let mut clip = Clip::blank("c1", "t1", ClipKind::Video, "picture", 0.0, 2.0);
        let (choices, old, index) = effect_choices(&clip, None, &presets).unwrap();
        assert_eq!((choices.len(), old, index), (2, None, None));
        let generated = GeneratedEffect { index: 0, entry: presets[0].clone() };
        clip.video_effects.push(generated.entry.clone());
        let (choices, old, index) =
            effect_choices(&clip, Some(&generated), &presets).unwrap();
        assert_eq!((choices.len(), old, index), (2, Some(0), Some(0)));
        assert_eq!(random_assignment(&[(2, old)], |_| 0), vec![1]);
        // A second effect authored by the user cannot be duplicated.
        clip.video_effects.push(presets[1].clone());
        let (choices, old, index) =
            effect_choices(&clip, Some(&generated), &presets).unwrap();
        assert_eq!((choices.len(), old, index), (1, Some(0), Some(0)));
        // If an author changes the generated parameters, it is theirs.
        clip.video_effects[0].params.insert("stops".into(), 1.0);
        assert!(effect_choices(&clip, Some(&generated), &presets).is_none());
    }

    #[test]
    fn media_type_scope_is_independent_and_never_includes_audio() {
        let image = Clip::blank("i", "t1", ClipKind::Image, "still", 0.0, 2.0);
        let video = Clip::blank("v", "t1", ClipKind::Video, "clip", 2.0, 2.0);
        let audio = Clip::blank("a", "t3", ClipKind::Audio, "narration", 0.0, 4.0);
        assert!(chosen_media_type(&image, true, false));
        assert!(!chosen_media_type(&video, true, false));
        assert!(!chosen_media_type(&image, false, true));
        assert!(chosen_media_type(&video, false, true));
        assert!(!chosen_media_type(&image, false, false));
        assert!(!chosen_media_type(&audio, true, true));
    }

    #[test]
    fn transform_bulk_protects_authored_keys_per_property() {
        let mut clip = Clip::blank("c1", "t1", ClipKind::Image, "still", 0.0, 2.0);
        assert!(matches!(bulk_transform_command(&clip, 0, 1.3, 0.0, 0.0),
            Some(Command::SetClipTransform { scale: Some(_), .. })));
        let keyed = ClipKey {
            property: KeyProperty::Scale, at: 0.0, value: 1.0,
            ease: concat_project::model::KeyEase::LINEAR,
        };
        clip.keys.push(keyed);
        assert!(bulk_transform_command(&clip, 0, 1.3, 0.0, 0.0).is_none());
        assert!(matches!(bulk_transform_command(&clip, 1, 1.0, 0.2, -0.2),
            Some(Command::SetClipTransform {
                offset_x: Some(_), offset_y: Some(_), scale: None, ..
            })));
        clip.keys.push(ClipKey { property: KeyProperty::OffsetX, ..keyed });
        assert!(matches!(bulk_transform_command(&clip, 1, 1.0, 0.2, -0.2),
            Some(Command::SetClipTransform {
                offset_x: None, offset_y: Some(_), ..
            })));
    }

    #[test]
    fn random_physics_controls_are_stable_and_media_specific() {
        let a = motion_controls("first-image");
        let b = motion_controls("second-image");
        let again = motion_controls("first-image");
        assert!((a.strength - again.strength).abs() < 1e-12);
        assert!((a.duration - again.duration).abs() < 1e-12);
        assert!((a.strength - b.strength).abs() > 1e-12
            || (a.speed - b.speed).abs() > 1e-12
            || (a.intensity - b.intensity).abs() > 1e-12);
        assert!((0.1..=2.0).contains(&a.strength));
        assert!((0.1..=1.0).contains(&a.duration));
        assert!((0.25..=3.0).contains(&a.speed));
        assert!((0.2..=2.0).contains(&a.intensity));
    }

    #[test]
    fn only_untouched_bulk_motion_can_be_rerolled() {
        let mut clip = Clip::blank("c1", "t1", ClipKind::Video, "picture", 0.0, 2.0);
        let (choices, old) = motion_choices(&clip, 1.0 / 30.0, &[], &MOTION_PRESETS).unwrap();
        assert_eq!(choices.len(), 9);
        assert_eq!(old, None);
        let generated = choices[0].clone();
        clip.keys = generated.keys.clone();
        assert!(motion_choices(&clip, 1.0 / 30.0, &[], &MOTION_PRESETS).is_none());
        assert_eq!(
            motion_choices(&clip, 1.0 / 30.0, &[generated.clone()], &MOTION_PRESETS).unwrap().1,
            Some(0)
        );
        clip.keys[1].value += 0.02;
        assert!(motion_choices(&clip, 1.0 / 30.0, &[generated], &MOTION_PRESETS).is_none());
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
