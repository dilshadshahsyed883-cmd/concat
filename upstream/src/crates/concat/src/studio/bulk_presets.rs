// SPDX-License-Identifier: AGPL-3.0-or-later

use std::collections::{HashMap, HashSet};
use std::hash::{BuildHasher, RandomState};
use std::path::Path;

use concat_project::model::{ClipKey, ClipKind, KeyProperty, VideoSettings};
use concat_project::physics_motion::{PhysicsControls, PhysicsPreset};
use concat_project::canvas_motion::{CanvasMotionControls, CanvasMotionPreset};

use super::{
    AppliedFilter, Catalogue, Clip, ClipPatch, Command, MotionPreset, PackageKind, Studio, Transition,
    gpu_catalogue_has, t, tf,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum MotionKind {
    Basic(MotionPreset),
    Physics(PhysicsPreset),
    Canvas(CanvasMotionPreset),
}

/// Favorites retain the six original physics indexes, then three existing basics,
/// then the Canvas Motion pack. IDs, rather than row numbers, are persisted.
pub(super) fn animation_favourite_id(index: i32) -> Option<&'static str> {
    match index {
        0..=5 => PhysicsPreset::from_index(index).map(PhysicsPreset::id),
        6 => Some("motion.push-in"), 7 => Some("motion.pan-right"), 8 => Some("motion.fade-in"),
        9..=29 => CanvasMotionPreset::from_index(index-9).map(CanvasMotionPreset::id),
        _ => None,
    }
}
fn animation_from_index(index: i32) -> Option<MotionKind> {
    match index {
        0..=5 => PhysicsPreset::from_index(index).map(MotionKind::Physics),
        6 => Some(MotionKind::Basic(MotionPreset::PushIn)),
        7 => Some(MotionKind::Basic(MotionPreset::PanRight)),
        8 => Some(MotionKind::Basic(MotionPreset::FadeIn)),
        _ => None,
    }
}
fn motion_id(preset: MotionKind) -> &'static str {
    match preset {
        MotionKind::Physics(p) => p.id(),
        MotionKind::Basic(MotionPreset::PushIn) => "motion.push-in",
        MotionKind::Basic(MotionPreset::PanRight) => "motion.pan-right",
        MotionKind::Basic(MotionPreset::FadeIn) => "motion.fade-in",
        MotionKind::Canvas(p) => p.id(),
    }
}

pub(super) fn canvas_controls(settings: &crate::ui::CanvasMotionData)->CanvasMotionControls {
    CanvasMotionControls {
            start_scale: f64::from(settings.start_scale), end_scale: f64::from(settings.end_scale),
            duration_seconds: f64::from(settings.duration), delay_seconds: f64::from(settings.delay),
            entry_seconds: f64::from(settings.entry_seconds), hold_seconds: f64::from(settings.hold_seconds),
            exit_seconds: f64::from(settings.exit_seconds),
            focus_middle: [f64::from(settings.focus_middle_x), f64::from(settings.focus_middle_y)],
            focus_hold_seconds: f64::from(settings.focus_hold),
            speed: f64::from(settings.speed), strength: f64::from(settings.strength),
            intensity: f64::from(settings.intensity), blur: f64::from(settings.blur), reverse: settings.reverse,
            start_anchor: settings.custom_anchors.then_some([f64::from(settings.start_x),f64::from(settings.start_y)]),
            end_anchor: settings.custom_anchors.then_some([f64::from(settings.end_x),f64::from(settings.end_y)]),
            easing: if settings.easing == 0 { concat_project::canvas_motion::MotionEasing::Linear } else { concat_project::canvas_motion::MotionEasing::Smooth },
            seed: settings.seed.max(0) as u64,
        }
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

fn generated_motion_command(clip: &Clip, generated: &GeneratedMotion,
    canvas_style: Option<(f64, Option<concat_project::model::ClipMask>)>) -> Command {
    Command::ApplyGeneratedMotion {
        clip_id: clip.id.clone(), expected_keys: clip.keys.clone(), keys: generated.keys.clone(),
        preset: motion_id(generated.preset).into(), canvas_style,
    }
}

/// Legacy session signatures supplement persisted per-clip ownership.
/// Authoritative ownership and undo history now live in the project snapshots.
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

pub(super) fn fresh_draw() -> impl FnMut(usize) -> usize {
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
    transition_length_with_default(previous, incoming, 0.9)
}

fn transition_length_with_default(
    previous: &Clip, incoming: &Clip, default_duration: f64,
) -> Option<(f64, CutSpan)> {
    let previous_end = previous.start + previous.duration;
    if (previous_end - incoming.start).abs() >= 0.5 / FPS
        || frame(previous_end) != frame(incoming.start)
    {
        return None;
    }
    let requested = incoming
        .transition_in
        .as_ref()
        .map_or(default_duration, |cut| cut.duration);
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

// Agent 4's Animations tab and Agent 3's transform tools share exactly
// the same type filter. Audio and title clips are never eligible.
fn requested_picture(clip: &Clip, images: bool, videos: bool) -> bool {
    chosen_media_type(clip, images, videos)
}

/// Vary random physics amplitude/timing independently per clip, while
/// retaining shared user control baselines and stable reroll recognition.
fn varied_physics_controls(base: PhysicsControls, clip_id: &str) -> PhysicsControls {
    let per_clip = motion_controls(clip_id);
    PhysicsControls {
        strength: (base.strength * per_clip.strength).clamp(0.1, 2.0),
        duration: (base.duration * per_clip.duration).clamp(0.1, 1.0),
        speed: (base.speed * per_clip.speed).clamp(0.25, 3.0),
        intensity: (base.intensity * per_clip.intensity).clamp(0.2, 2.0),
    }
}

fn chosen_media_type(clip: &Clip, images: bool, videos: bool) -> bool {
    match clip.kind {
        ClipKind::Image => images && !clip.extra.contains_key("canvasOwner"),
        ClipKind::Video => videos && !clip.extra.contains_key("canvasOwner"),
        _ => false,
    }
}

/// Return at most one transform edit per clip. A manually animated property
/// is skipped independently; applying scale never overwrites animated X/Y.
fn bulk_transform_command(
    clip: &Clip, mode: i32, scale: f64, x: f64, y: f64, rotation: f64,
) -> Option<Command> {
    let scale = (matches!(mode, 0 | 3)
        && !clip.is_keyed(KeyProperty::Scale)
        && (clip.scale - scale).abs() > 1e-9).then_some(scale);
    let offset_x = (matches!(mode, 1 | 3)
        && !clip.is_keyed(KeyProperty::OffsetX)
        && (clip.offset_x - x).abs() > 1e-9).then_some(x);
    let offset_y = (matches!(mode, 1 | 3)
        && !clip.is_keyed(KeyProperty::OffsetY)
        && (clip.offset_y - y).abs() > 1e-9).then_some(y);
    let rotation = (matches!(mode, 2 | 3) && !clip.is_keyed(KeyProperty::Rotation)
        && (clip.rotation - rotation).abs() > 1e-9).then_some(rotation);
    if scale.is_none() && offset_x.is_none() && offset_y.is_none() && rotation.is_none() {
        return None;
    }
    Some(Command::SetClipTransform {
        clip_id: clip.id.clone(),
        scale,
        offset_x,
        offset_y,
        rotation,
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
    motion_choices_with_controls(clip, frame, generated, permitted, None)
}

fn motion_choices_with_controls(
    clip: &Clip,
    frame: f64,
    generated: &[GeneratedMotion],
    permitted: &[MotionKind],
    controls: Option<PhysicsControls>,
) -> Option<(Vec<GeneratedMotion>, Option<usize>)> {
    let previous = generated
        .iter()
        .find(|motion| clip.keys.as_slice() == motion.keys.as_slice());
    let base = concat_project::motion_preset::preset_base(
        clip, generated.iter().map(|motion| motion.keys.as_slice()),
    )?;
    let choices: Vec<_> = MOTION_PRESETS
        .into_iter()
        .filter(|preset| permitted.contains(preset))
        .filter_map(|preset| {
            let (keys, _) = match preset {
                MotionKind::Basic(basic) => {
                    let c = controls.unwrap_or_default();
                    let (keys, preview) = basic.keys_with(&base, frame, c.strength, c.duration, c.speed).ok()?;
                    (keys.to_vec(), preview)
                }
                // Vary physics per clip, with stable parameters so a reroll
                // recognizes exactly the keys that the previous bulk action owns.
                MotionKind::Physics(physics) => physics.keys_with(
                    &base, frame, controls.unwrap_or_else(|| motion_controls(&clip.id)),
                ).ok()?,
                MotionKind::Canvas(preset) => (preset.keys(&base, frame, CanvasMotionControls::default()).ok()?, base.start + base.duration/2.0),
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

/// Recognize the previous random choice without treating its presence as an
/// application conflict. Updates address one effect type and retain the others.
fn effect_choices(
    clip: &Clip,
    saved: Option<&GeneratedEffect>,
    presets: &[AppliedFilter],
) -> Option<(Vec<AppliedFilter>, Option<usize>, Option<usize>)> {
    let managed = saved.filter(|held| clip.video_effects.get(held.index) == Some(&held.entry));
    let managed_index = managed.map(|held| held.index);
    // Explicit application updates the addressed type; its earlier presence
    // is not a conflict. Other effect types stay in the stack.
    let choices:Vec<_>=presets.to_vec();
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
                                ext.eq_ignore_ascii_case("jpg") || ext.eq_ignore_ascii_case("jpeg") || ext.eq_ignore_ascii_case("png")
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
                    && let Some((duration, span)) = transition_length_with_default(
                        previous, incoming,
                        self.transition_draft_duration,
                    )
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

        let saved_intensity = self.transition_draft_intensity;
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
                    intensity: saved_intensity,
                    params: Default::default(),
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
        self.apply_random_gpu_effects_scoped(true,true,false);
    }
    pub fn apply_random_gpu_effects_scoped(&mut self,images:bool,videos:bool,selected_only:bool) {
        if !images && !videos {self.notify("Choose Images or Video Clips first.",true);return;}
        let selected:HashSet<_>=self.selection.iter().cloned().collect();
        if selected_only && selected.is_empty() {self.notify("Select eligible timeline media first.",true);return;}
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
                chosen_media_type(clip,images,videos)
                    && (!selected_only || selected.contains(&clip.id))
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
        for ((clip, target, choices, _old, _managed_index), chosen) in plans
            .into_iter().zip(random_assignment(&options,fresh_draw())) {
            let entry=choices[chosen].clone();
            let mut effects=clip.video_effects.clone();
            concat_project::model::upsert_effect(&mut effects,entry.clone());
            if effects==clip.video_effects {continue;}
            let index=effects.iter().position(|applied|applied.id==entry.id).unwrap_or(0);
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

    pub fn animation_settings(&self,index:i32)->concat_project::animation_settings::Settings {
        let value=animation_favourite_id(index).and_then(|id|self.prefs.animation_preset_settings.get(id));
        concat_project::animation_settings::Settings::from_value(value)
    }
    pub fn remember_animation_settings(&mut self,index:i32,strength:f32,duration:f32,speed:f32,intensity:f32,canvas:crate::ui::CanvasMotionData) {
        let Some(id)=animation_favourite_id(index) else {return;};
        let mut saved=self.animation_settings(index);
        if index>=9 {saved.canvas=canvas_controls(&canvas);saved.corners=f64::from(canvas.corner_radius).clamp(0.0,0.2);}
        else {saved.physics=PhysicsControls {strength:f64::from(strength),duration:f64::from(duration),speed:f64::from(speed),intensity:f64::from(intensity)};}
        if !saved.valid() {return;}
        if let Ok(value)=serde_json::to_value(saved) {
            if self.prefs.animation_preset_settings.get(id)!=Some(&value) {
                self.prefs.animation_preset_settings.insert(id.into(),value);
                self.prefs.save(&self.host.dirs);
            }
        }
    }
    /// Report whether a preset has a stored override, distinct from draft edits.
    pub fn has_animation_default(&self,index:i32)->bool {
        animation_favourite_id(index).is_some_and(|id|self.prefs.animation_preset_settings.contains_key(id))
    }
    /// Reset factory values by removing this preset's saved override only.
    /// Applied clip animation keys are not part of preferences.
    pub fn reset_animation_settings(&mut self,index:i32) {
        let Some(id)=animation_favourite_id(index) else {return;};
        if self.prefs.animation_preset_settings.remove(id).is_some() {
            self.prefs.save(&self.host.dirs);
        }
    }
    /// Each chosen starred preset uses its own configured parameters. Timeline
    /// selection and preset pool selection are independent, with no empty fallback.
    /// The explicit No Animation choice is a scoped, undoable clear.
    /// Only exact persisted generated ownership is eligible.  Unowned/manual
    /// keys, hidden/locked lanes and non-GPU picture targets are never touched.
    pub fn clear_generated_motion_bulk(&mut self, images: bool, videos: bool, selected_only: bool) {
        if !images && !videos {
            self.notify("Choose Images or Video Clips first.", true);
            return;
        }
        if !self.gpu_bulk_ready() { return; }
        self.flush_commit();
        let selected: HashSet<_> = self.selection.iter().cloned().collect();
        if selected_only && selected.is_empty() {
            self.notify("Select timeline media before clearing an animation.", true);
            return;
        }
        let media = self.gpu_picture_media();
        let commands: Vec<_> = self.timeline().clips.iter()
            .filter(|clip| requested_picture(clip, images, videos)
                && (!selected_only || selected.contains(&clip.id))
                && !self.locked(&clip.track_id)
                && self.timeline().track(&clip.track_id).is_some_and(|track| track.visible)
                && media.contains(clip.media_id.as_str())
                && concat_project::motion_preset::generated_ownership(clip).is_some())
            .map(|clip| Command::ClearGeneratedMotion {
                clip_id: clip.id.clone(),
                expected_keys: clip.keys.clone(),
            }).collect();
        if commands.is_empty() {
            self.notify("No owned generated animations to clear; manual keys and protected tracks are unchanged.", false);
            return;
        }
        let count = commands.len();
        self.apply(Command::Batch { commands });
        self.notify(&format!("Cleared {count} owned animation(s); manual keys and unrelated settings preserved."), false);
    }

    pub fn apply_animation_favourites(&mut self,pool:Vec<i32>,images:bool,videos:bool,selected_only:bool) {
        if !images && !videos {self.notify("Choose Images or Video Clips first.",true);return;}
        if !self.gpu_bulk_ready() {return;}
        let permitted:Vec<_>=pool.into_iter().filter_map(|index|{
            let id=animation_favourite_id(index)?;
            if !self.prefs.physics_motion_favourites.iter().any(|f|f==id) {return None;}
            let preset=if index>=9 {MotionKind::Canvas(CanvasMotionPreset::from_index(index-9)?)} else {animation_from_index(index)?};
            Some((preset,self.animation_settings(index)))
        }).collect();
        if permitted.is_empty() {self.notify("Select at least one starred animation for the Favorites pool.",true);return;}
        self.flush_commit();
        let selected:HashSet<_>=self.selection.iter().cloned().collect();
        if selected_only && selected.is_empty() {self.notify("Select eligible timeline media first.",true);return;}
        let media=self.gpu_picture_media();let frame=self.frame_seconds();
        let mut plans=Vec::new();
        for clip in &self.timeline().clips {
            if !chosen_media_type(clip,images,videos) || (selected_only && !selected.contains(&clip.id))
                || self.locked(&clip.track_id) || self.timeline().track(&clip.track_id).is_none_or(|t|!t.visible)
                || !media.contains(clip.media_id.as_str()) {continue;}
            let Some(base)=concat_project::motion_preset::preset_base(clip,std::iter::empty()) else {continue;};
            let choices:Vec<_>=permitted.iter().filter_map(|(preset,settings)|{
                let (keys,style)=match preset {
                    MotionKind::Basic(p)=>{let c=settings.physics;(p.keys_with(&base,frame,c.strength,c.duration,c.speed).ok()?.0.to_vec(),None)}
                    MotionKind::Physics(p)=>(p.keys_with(&base,frame,settings.physics).ok()?.0,None),
                    MotionKind::Canvas(p)=>{
                        let mut controls=settings.canvas;
                        controls.seed=clip.id.bytes().fold(controls.seed,|h,b|h.wrapping_mul(31).wrapping_add(u64::from(b)));
                        let mask=if settings.corners>0.0 {self.project().media_by_id(&clip.media_id).and_then(|m|Some((m.width?,m.height?)))
                            .and_then(|(w,h)|concat_project::canvas_motion::rounded_mask(w,h,settings.corners))}else{None};
                        (p.keys(&base,frame,controls).ok()?,Some((controls.blur,mask)))
                    }
                };
                Some((GeneratedMotion {preset:*preset,keys},style))
            }).collect();
            if !choices.is_empty() {plans.push((clip,choices));}
        }
        let mut draw=fresh_draw();let mut commands=Vec::new();
        for (clip,choices) in plans {
            let (generated,style)=&choices[draw(choices.len())];
            commands.push(generated_motion_command(clip,generated,*style));
        }
        if commands.is_empty(){self.notify("No eligible targets; manual keys and locked/hidden tracks are protected.",true);return;}
        self.apply(Command::Batch {commands});
    }

    pub(super) fn publish_animation_timing(&self,app:&crate::ui::App) {
        use slint::ComponentHandle;
        let ui=app.global::<crate::ui::Keyframes>();
        let index=ui.get_animation_selected();
        if index < 0 {
            ui.set_animation_timing("No Animation selected. Apply Selected or Apply All clears only owned generated motion.".into());
            return;
        }
        let physics=PhysicsControls {strength:f64::from(ui.get_physics_strength()),duration:f64::from(ui.get_physics_duration()),speed:f64::from(ui.get_physics_speed()),intensity:f64::from(ui.get_physics_intensity())};
        let canvas=canvas_controls(&ui.get_canvas_motion());
        let mut seconds=Vec::new();
        for clip in &self.timeline().clips {
            if !chosen_media_type(clip,ui.get_animation_images(),ui.get_animation_videos())
                || (ui.get_animation_scope()==1 && !self.selection.contains(&clip.id)) || self.locked(&clip.track_id)
                || self.timeline().track(&clip.track_id).is_none_or(|t|!t.visible) {continue;}
            let Some(base)=concat_project::motion_preset::preset_base(clip,std::iter::empty()) else {continue;};
            let keys=if index>=9 {CanvasMotionPreset::from_index(index-9).and_then(|p|p.keys(&base,self.frame_seconds(),canvas).ok())}
                else {animation_from_index(index).and_then(|p|match p {
                    MotionKind::Basic(p)=>p.keys_with(&base,self.frame_seconds(),physics.strength,physics.duration,physics.speed).ok().map(|k|k.0.to_vec()),
                    MotionKind::Physics(p)=>p.keys_with(&base,self.frame_seconds(),physics).ok().map(|k|k.0),_=>None})};
            if let Some(mut keys)=keys {
                keys.sort_by(|a,b|(a.property as u8).cmp(&(b.property as u8)).then_with(||a.at.total_cmp(&b.at)));
                let end=keys.windows(2).filter(|pair|pair[0].property==pair[1].property && (pair[0].value-pair[1].value).abs()>1e-10)
                    .map(|pair|pair[1].at*clip.duration).fold(0.0,f64::max);
                seconds.push(end);
            }
        }
        let min=seconds.iter().copied().fold(f64::INFINITY,f64::min);
        let max=seconds.iter().copied().fold(0.0,f64::max);
        let label=if seconds.is_empty() {"Effective timing: no eligible media in this scope.".into()}
            else if max-min>0.005 {format!("Effective motion ends at {min:.2}–{max:.2} s across {} eligible clips (visible-frame timing).",seconds.len())}
            else {format!("Effective motion ends at {max:.2} s (visible-frame timing, including delay/hold where applicable).")};
        ui.set_animation_timing(label.into());
    }

    pub fn apply_physics_bulk(
        &mut self, index: i32, mode: i32, images: bool, videos: bool,
        selected_only: bool, strength: f32, duration: f32, speed: f32, intensity: f32,
    ) {
        if !images && !videos {
            self.notify("Select Images or Video Clips before applying animations.", true);
            return;
        }
        if !self.gpu_bulk_ready() {
            return;
        }
        let permitted: Vec<MotionKind> = match mode {
            0 => animation_from_index(index)
                .map(|preset| vec![preset])
                .unwrap_or_default(),
            2 => MOTION_PRESETS.into_iter().collect(),
            3 => MOTION_PRESETS.into_iter()
                .filter(|preset| self.prefs.physics_motion_favourites
                    .iter().any(|id| id == motion_id(*preset)))
                .collect(),
            _ => return,
        };
        if permitted.is_empty() {
            self.notify("Choose a valid animation or star a preset first.", true);
            return;
        }
        self.flush_commit();
        let selected: HashSet<String> = self.selection.iter().cloned().collect();
        if selected_only && selected.is_empty() {
            self.notify("Select at least one timeline clip first.", true);
            return;
        }
        let controls = PhysicsControls {
            strength: strength as f64,
            duration: duration as f64,
            speed: speed as f64,
            intensity: intensity as f64,
        };
        let timeline = self.timeline();
        let frame = self.frame_seconds();
        let media = self.gpu_picture_media();
        let tracks: HashSet<_> = timeline.tracks.iter()
            .filter(|track| track.visible && !self.locked(&track.id))
            .map(|track| track.id.as_str())
            .collect();
        let mut plans: Vec<_> = timeline.clips.iter()
            .filter(|clip| requested_picture(clip, images, videos)
                && tracks.contains(clip.track_id.as_str())
                && media.contains(clip.media_id.as_str())
                && (!selected_only || selected.contains(&clip.id)))
            .filter_map(|clip| {
                let target = (timeline.id.clone(), clip.id.clone());
                let held = self.bulk_motion.0.get(&target)
                    .map_or(&[][..], Vec::as_slice);
                let (choices, old) =
                    motion_choices_with_controls(clip, frame, held, &permitted,
                        Some(if mode == 2 || mode == 3 {
                            varied_physics_controls(controls, &clip.id)
                        } else {
                            controls
                        }))?;
                Some((clip, target, choices, old))
            })
            .collect();
        if plans.is_empty() {
            self.notify(&t("studio.randomNoGpuMotion"), true);
            return;
        }
        // Timeline order is canonical for adjacent-favourite avoidance.
        plans.sort_by(|a, b| a.0.start.total_cmp(&b.0.start)
            .then_with(|| a.0.id.cmp(&b.0.id)));
        let indices: Vec<usize> = if mode == 0 {
            vec![0; plans.len()]
        } else {
            let options: Vec<_> = plans.iter()
                .map(|(_, _, choices, old)| (choices.len(), *old))
                .collect();
            random_assignment(&options, fresh_draw())
        };
        let mut updates = Vec::new();
        let mut commands = Vec::new();
        let mut last_preset = None;
        for ((clip, target, choices, old), mut chosen) in plans.into_iter().zip(indices) {
            if mode == 3 && Some(choices[chosen].preset) == last_preset {
                if let Some(alternative) = choices.iter().position(|item|
                    Some(item.preset) != last_preset
                    && Some(item.preset) != old.and_then(|i| choices.get(i).map(|x| x.preset))) {
                    chosen = alternative;
                }
            }
            last_preset = Some(choices[chosen].preset);
            if old == Some(chosen) {
                continue;
            }
            let generated = choices[chosen].clone();
            commands.push(generated_motion_command(clip, &generated, None));
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

    /// Native multi-channel inset motion, with independent backdrop and one undo step.
    pub fn apply_canvas_motion_bulk(&mut self, index: i32, mode: i32, images: bool,
        videos: bool, selected_only: bool, settings: crate::ui::CanvasMotionData) {
        let controls = canvas_controls(&settings);
        if !settings.corner_radius.is_finite() || !(0.0..=0.2).contains(&settings.corner_radius) {self.notify("Invalid foreground corner radius.",true);return;}
        if !images && !videos { self.notify("Select Images or Video Clips first.", true); return; }
        if !controls.valid() || !matches!(mode, 0 | 2 | 3) { self.notify("Invalid Canvas Motion settings.", true); return; }
        if !self.gpu_bulk_ready() { return; }
        let permitted: Vec<_> = CanvasMotionPreset::ALL.into_iter().filter(|preset|
            (mode != 0 || Some(*preset) == CanvasMotionPreset::from_index(index))
            && (mode != 3 || self.prefs.physics_motion_favourites.iter().any(|id| id == preset.id()))).collect();
        if permitted.is_empty() { self.notify("Choose a Canvas Motion preset or star one first.", true); return; }
        let selected: HashSet<_> = self.selection.iter().cloned().collect();
        if selected_only && selected.is_empty() { self.notify("Select at least one timeline clip.", true); return; }
        let timeline = self.timeline(); let frame = self.frame_seconds(); let media = self.gpu_picture_media();
        let mut plans = Vec::new();
        for clip in &timeline.clips {
            if !requested_picture(clip, images, videos) || self.locked(&clip.track_id)
                || timeline.track(&clip.track_id).is_none_or(|t| !t.visible)
                || !media.contains(clip.media_id.as_str()) || (selected_only && !selected.contains(&clip.id)) { continue; }
            let target = (timeline.id.clone(), clip.id.clone());
            let previous = self.bulk_motion.0.get(&target).and_then(|held|
                held.iter().find(|held| held.keys == clip.keys));
            let held = self.bulk_motion.0.get(&target).map_or(&[][..], Vec::as_slice);
            let Some(base) = concat_project::motion_preset::preset_base(clip, held.iter().map(|motion| motion.keys.as_slice())) else { continue; };
            let mut per_clip = controls;
            per_clip.seed = clip.id.bytes().fold(controls.seed.wrapping_add(0xcbf29ce484222325), |h,b| (h ^ u64::from(b)).wrapping_mul(0x100000001b3));
            let choices: Vec<_> = permitted.iter().filter_map(|preset|
                preset.keys(&base, frame, per_clip).ok().map(|keys|
                    GeneratedMotion { preset: MotionKind::Canvas(*preset), keys })).collect();
            if choices.is_empty() { continue; }
            let old = previous.and_then(|old| choices.iter().position(|candidate| candidate == old));
            plans.push((clip, target, choices, old));
        }
        plans.sort_by(|a,b|a.0.start.total_cmp(&b.0.start).then_with(||a.0.id.cmp(&b.0.id)));
        let picks = if mode == 0 { vec![0; plans.len()] } else {
            random_assignment(&plans.iter().map(|p|(p.2.len(),p.3)).collect::<Vec<_>>(), fresh_draw())
        };
        let mut commands = Vec::new(); let mut updates = Vec::new();
        for ((clip, target, choices, _old), chosen) in plans.into_iter().zip(picks) {
            let generated = choices[chosen].clone();
            let mask = if settings.corner_radius > 0.0 {
                self.project().media_by_id(&clip.media_id).and_then(|m| Some((m.width?, m.height?)))
                    .and_then(|(w,h)| concat_project::canvas_motion::rounded_mask(w,h,f64::from(settings.corner_radius).min(0.2)))
            } else { None };
            // Style-only changes are real updates even when movement keys match.
            commands.push(generated_motion_command(clip, &generated, Some((controls.blur, mask))));
            updates.push((target,generated));
        }
        if commands.is_empty() {self.notify("No eligible media. Manual or unrecognized keyframes are protected; check targets, selection and locked/hidden tracks.",true);return;}
        let revision=self.revision;self.apply(Command::Batch {commands});
        if self.revision!=revision {for (target,motion) in updates {self.bulk_motion.remember(target,motion);}}
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
        self.apply_bulk_gpu_motion_with_preset(mode, 0, images, videos, selected_only);
    }

    /// For Apply-to-All the caller selects one catalogue animation (0–8);
    /// random modes ignore the index. Agent 4 can reuse this contract.
    pub fn apply_bulk_gpu_motion_with_preset(
        &mut self, mode: i32, preset: i32,
        images: bool, videos: bool, selected_only: bool,
    ) {
        if mode == 0 && !(0..MOTION_PRESETS.len() as i32).contains(&preset) {
            self.notify("Choose a supported animation preset.", true);
            return;
        }
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
            .filter(|preset| !favourites_only || self.prefs.physics_motion_favourites
                .iter().any(|id| id == motion_id(*preset)))
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
        // On short clips a preset may have no valid keyframe span.
        // Omit only that target rather than misindexing another animation.
        let chosen_preset = (mode == 0).then(|| MOTION_PRESETS[preset as usize]);
        plans.retain(|(_, _, choices, _)| chosen_preset.is_none_or(|wanted|
            choices.iter().any(|motion| motion.preset == wanted)));
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
        let picks = if let Some(wanted) = chosen_preset {
            plans.iter().map(|(_, _, choices, _)|
                choices.iter().position(|entry| entry.preset == wanted).unwrap_or(0))
                .collect()
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
            commands.push(generated_motion_command(clip, &generated, None));
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
    /// Explicit transform/orientation replacement with the shared target semantics.
    pub fn apply_adjustment_bulk(&mut self, page:i32, images:bool, videos:bool, selected_only:bool,
        scale:f32,x:f32,y:f32,rotation:f32,opacity:f32,flip_h:bool,flip_v:bool) {
        if !images && !videos {self.notify("Choose Images or Video Clips first.",true);return;}
        if !matches!(page,0|1|3) || ![scale,x,y,rotation,opacity].iter().all(|v|v.is_finite())
            || !(0.05..=8.0).contains(&scale) || !(-1.0..=1.0).contains(&x)
            || !(-1.0..=1.0).contains(&y) || !(-180.0..=180.0).contains(&rotation)
            || !(0.0..=1.0).contains(&opacity) {self.notify("Invalid adjustment settings.",true);return;}
        self.flush_commit();
        let selected:HashSet<_>=self.selection.iter().cloned().collect();
        if selected_only && selected.is_empty() {self.notify("Select eligible timeline media first.",true);return;}
        let mut commands=Vec::new();
        for clip in &self.timeline().clips {
            if !chosen_media_type(clip,images,videos) || (selected_only && !selected.contains(&clip.id))
                || self.locked(&clip.track_id) || self.timeline().track(&clip.track_id).is_none_or(|t|!t.visible)
                || self.project().media_by_id(&clip.media_id).is_none_or(|m|m.placeholder) {continue;}
            if page==3 {
                if clip.kind!=ClipKind::Video || concat_project::canvas_viewport::Viewport::of(clip).is_none() {continue;}
                if let Some(command)=bulk_transform_command(clip,3,f64::from(scale),f64::from(x),f64::from(y),clip.rotation) {commands.push(command);}
            } else if page==0 {
                if let Some(command)=bulk_transform_command(clip,3,f64::from(scale),f64::from(x),f64::from(y),f64::from(rotation)) {commands.push(command);}
                if !clip.is_keyed(KeyProperty::Opacity) && clip.opacity!=f64::from(opacity) {
                    commands.push(Command::UpdateClip {clip_id:clip.id.clone(),patch:ClipPatch {opacity:Some(f64::from(opacity)),..Default::default()}});
                }
            } else if clip.flip_h!=flip_h || clip.flip_v!=flip_v {
                commands.push(Command::UpdateClip {clip_id:clip.id.clone(),patch:ClipPatch {flip_h:Some(flip_h),flip_v:Some(flip_v),..Default::default()}});
            }
        }
        if commands.is_empty() {self.notify("No eligible changes; check targets and authored keyframes.",false);return;}
        self.apply(Command::Batch {commands});
    }

    pub fn apply_bulk_gpu_transform(
        &mut self, mode: i32, images: bool, videos: bool,
        selected_only: bool, scale: f32, x: f32, y: f32, rotation: f32,
    ) {
        if !images && !videos {
            self.notify("Choose Images, Video Clips, or both before bulk editing.", true);
            return;
        }
        if !matches!(mode, 0..=5) {
            self.notify("Unknown bulk transform action.", true);
            return;
        }
        let (scale, x, y, rotation) = (f64::from(scale), f64::from(x), f64::from(y), f64::from(rotation));
        if !scale.is_finite() || !x.is_finite() || !y.is_finite() || !rotation.is_finite()
            || !(-180.0..=180.0).contains(&rotation)
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
        self.flush_commit();
        let timeline = self.timeline();
        let media: HashSet<_> = self.project().media.iter().filter(|m| !m.placeholder).map(|m| m.id.as_str()).collect();
        let tracks: HashSet<_> = timeline.tracks.iter()
            .filter(|track| track.visible && !self.locked(&track.id))
            .map(|track| track.id.as_str())
            .collect();
        let commands: Vec<_> = timeline.clips.iter()
            .filter(|clip| chosen_media_type(clip, images, videos)
                && (!selected_only || selected.contains(&clip.id))
                && tracks.contains(clip.track_id.as_str())
                && media.contains(clip.media_id.as_str()))
            .filter_map(|clip| {
                if mode >= 4 {
                    let item=self.project().media_by_id(&clip.media_id)?;
                    if [KeyProperty::Scale,KeyProperty::OffsetX,KeyProperty::OffsetY].into_iter().any(|k|clip.is_keyed(k))
                        || item.width.unwrap_or(0)==0 || item.height.unwrap_or(0)==0 { return None; }
                    Some(Command::FrameClip {clip_id:clip.id.clone(),framing:if mode==4 {concat_project::model::Framing::Fit}else{concat_project::model::Framing::Fill},at:0.0})
                } else {bulk_transform_command(clip, mode, scale, x, y, rotation)}
            })
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
        assert_eq!((choices.len(), old, index), (2, Some(0), Some(0)));
        // If an author changes the generated parameters, it is theirs.
        clip.video_effects[0].params.insert("stops".into(), 1.0);
        let (choices,old,index)=effect_choices(&clip,Some(&generated),&presets).unwrap();
        assert_eq!((choices.len(),old,index),(2,None,None));
    }

    #[test]
    fn physics_bulk_target_checks_both_media_types_independently() {
        let mut clip = Clip::blank("v1", "t1", ClipKind::Video, "video", 0.0, 2.0);
        assert!(requested_picture(&clip, false, true));
        assert!(!requested_picture(&clip, true, false));
        assert!(!requested_picture(&clip, false, false));
        clip.kind = ClipKind::Image;
        assert!(requested_picture(&clip, true, false));
        assert!(!requested_picture(&clip, false, true));
        clip.kind = ClipKind::Audio;
        assert!(!requested_picture(&clip, true, true));
    }


    #[test]
    fn randomized_physics_respects_user_settings_with_per_clip_variation() {
        let base = PhysicsControls {
            strength: 1.2, duration: 0.8, speed: 1.3, intensity: 1.1,
        };
        let a = varied_physics_controls(base, "clip-001");
        let b = varied_physics_controls(base, "clip-002");
        assert!(a.strength != b.strength || a.speed != b.speed || a.intensity != b.intensity);
        assert_eq!(a.strength, varied_physics_controls(base, "clip-001").strength);
        assert!((0.1..=2.0).contains(&a.strength));
        assert!((0.1..=1.0).contains(&a.duration));
        assert!((0.25..=3.0).contains(&a.speed));
        assert!((0.2..=2.0).contains(&a.intensity));
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
        assert!(matches!(bulk_transform_command(&clip, 0, 1.3, 0.0, 0.0, 0.0),
            Some(Command::SetClipTransform { scale: Some(_), .. })));
        let keyed = ClipKey {
            property: KeyProperty::Scale, at: 0.0, value: 1.0,
            ease: concat_project::model::KeyEase::LINEAR,
        };
        clip.keys.push(keyed);
        assert!(bulk_transform_command(&clip, 0, 1.3, 0.0, 0.0, 0.0).is_none());
        assert!(matches!(bulk_transform_command(&clip, 1, 1.0, 0.2, -0.2, 0.0),
            Some(Command::SetClipTransform {
                offset_x: Some(_), offset_y: Some(_), scale: None, ..
            })));
        clip.keys.push(ClipKey { property: KeyProperty::OffsetX, ..keyed });
        assert!(matches!(bulk_transform_command(&clip, 1, 1.0, 0.2, -0.2, 0.0),
            Some(Command::SetClipTransform {
                offset_x: None, offset_y: Some(_), ..
            })));
    }

    #[test]
    fn combined_transform_preserves_animated_rotation_and_typed_targeting() {
        let mut clip=Clip::blank("i","T1",ClipKind::Image,"still",0.0,2.0);
        clip.keys.push(ClipKey {property:KeyProperty::Rotation,at:0.0,value:15.0,ease:concat_project::model::KeyEase::LINEAR});
        let command=bulk_transform_command(&clip,3,1.5,0.1,-0.2,90.0).unwrap();
        assert!(matches!(command,Command::SetClipTransform {scale:Some(1.5),offset_x:Some(0.1),offset_y:Some(-0.2),rotation:None,..}));
        assert!(bulk_transform_command(&clip,2,1.0,0.0,0.0,90.0).is_none());
        clip.keys.clear();
        assert!(matches!(bulk_transform_command(&clip,2,1.0,0.0,0.0,90.0),Some(Command::SetClipTransform {scale:None,rotation:Some(90.0),..})));
        clip.extra.insert("canvasOwner".into(),serde_json::json!("original"));
        assert!(!chosen_media_type(&clip,true,true));
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
        assert_eq!(length, 0.9);
        assert_eq!((span.start, span.end), (17, 44));
        let (remembered, _) = transition_length_with_default(&previous, &incoming, 0.7).unwrap();
        assert!((remembered - 0.7).abs() < 1e-12);
        let (clamped, _) = transition_length_with_default(&previous, &incoming, 2.0).unwrap();
        assert_eq!(clamped, 1.0);
        incoming.kind = ClipKind::Video;
        let (_, span) = transition_length(&previous, &incoming).unwrap();
        assert_eq!((span.start, span.end), (30, 57));
        incoming.start = 1.1;
        assert!(transition_length(&previous, &incoming).is_none());
        incoming.start = 1.0;
        incoming.duration = 1.0 / 30.0;
        assert!(transition_length(&previous, &incoming).is_none());
    }
}
