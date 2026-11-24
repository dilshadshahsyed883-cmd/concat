// SPDX-License-Identifier: AGPL-3.0-or-later

//! Inspector motion presets expressed as ordinary, undoable clip keys.

use crate::Command;
use crate::model::{Clip, ClipKey, ClipKind, KeyEase, KeyProperty, ranges};

/// Return an unkeyed working copy only when every existing key is recognized.
/// All preset families share this rule. Persisted ownership recognizes generated
/// work after reopen; keys without ownership or an exact session signature stay protected.
pub fn preset_base<'a, 's>(
    clip: &'a Clip,
    signatures: impl IntoIterator<Item = &'s [ClipKey]>,
) -> Option<std::borrow::Cow<'a, Clip>> {
    if clip.keys.is_empty() {
        Some(std::borrow::Cow::Borrowed(clip))
    } else if generated_ownership(clip).is_some()
        || signatures
            .into_iter()
            .any(|keys| keys == clip.keys.as_slice())
    {
        let mut clean = clip.clone();
        clean.keys.clear();
        Some(std::borrow::Cow::Owned(clean))
    } else {
        None
    }
}

/// Ownership is saved with the clip and therefore travels through document IO,
/// undo snapshots and split copies. A changed key vector is always manual work.
pub const OWNERSHIP_KEY: &str = "generatedMotionV1";

/// Versioned generated-motion metadata stored in the clip's extension map.
#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct GeneratedOwnership {
    /// Metadata format version; unknown versions are never assumed owned.
    pub version: u32,
    /// Stable catalogue ID.
    pub preset: String,
    /// Canonical keys last produced by this feature.
    pub keys: Vec<ClipKey>,
    /// Style ownership, separate from key ownership.
    pub style: Option<GeneratedStyle>,
}

/// Original and generated values for independently owned styling channels.
#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct GeneratedStyle {
    /// Whether this feature actually owns the background channel.
    #[serde(default)]
    pub owns_background: bool,
    /// An existing authored mask is never claimed by the motion feature.
    #[serde(default)]
    pub owns_mask: bool,
    /// Background before Canvas Motion first changed it.
    pub before_background: Option<crate::model::Background>,
    /// Last background produced by Canvas Motion.
    pub applied_background: Option<crate::model::Background>,
    /// Mask before Canvas Motion first changed it.
    pub before_mask: Option<crate::model::ClipMask>,
    /// Last mask produced by Canvas Motion.
    pub applied_mask: Option<crate::model::ClipMask>,
}

fn saved_ownership(clip: &Clip) -> Option<GeneratedOwnership> {
    let saved: GeneratedOwnership =
        serde_json::from_value(clip.extra.get(OWNERSHIP_KEY)?.clone()).ok()?;
    (saved.version == 1).then_some(saved)
}

/// Recognize only the exact generated keys, never a later manual edit.
pub fn generated_ownership(clip: &Clip) -> Option<GeneratedOwnership> {
    saved_ownership(clip).filter(|saved| saved.keys == clip.keys)
}

/// Canvas layout and Canvas Motion are mutually exclusive; Basic/Physics
/// still use the clip's ordinary content transform inside a fixed aperture.
/// Recognize the persisted preset marker as well as the current viewport so
/// older projects retain the same priority even before a layout is reapplied.
pub fn has_canvas_preset(clip: &Clip) -> bool {
    clip.extra.contains_key("canvasPreset")
        || clip.extra.contains_key(crate::canvas_viewport::KEY)
}

/// Stable catalogue IDs, not UI row indexes or mutable style, identify family.
pub fn is_canvas_animation(preset: &str) -> bool {
    preset.starts_with("canvas-motion.")
}

/// A manually changed generated Canvas animation must not be silently erased.
/// Retain the saved record even if its current key signature no longer matches.
pub fn edited_canvas_animation(clip: &Clip) -> bool {
    saved_ownership(clip).is_some_and(|saved|
        is_canvas_animation(&saved.preset) && saved.keys != clip.keys)
}

/// Remove only recognized Canvas Animation; preserve Basic/Physics and authored
/// motion. This is called inside the staged Canvas preset transaction.
pub fn clear_canvas_animation_for_preset(clip: &mut Clip) -> Result<bool, crate::CommandError> {
    if edited_canvas_animation(clip) {
        return Err(crate::CommandError::UnsafeEdit {
            reason: "Canvas Animation has manually edited keyframes; resolve those edits before applying a Canvas Preset.".into(),
        });
    }
    if generated_ownership(clip).is_some_and(|saved| is_canvas_animation(&saved.preset)) {
        let expected = clip.keys.clone();
        return clear_generated(clip, &expected);
    }
    Ok(false)
}

/// Rewindowing owned keys is an editorial operation, not manual key authoring.
pub(crate) fn refresh_ownership(clip: &mut Clip, mut saved: GeneratedOwnership) {
    saved.keys = clip.keys.clone();
    if let Ok(value) = serde_json::to_value(saved) {
        clip.extra.insert(OWNERSHIP_KEY.into(), value);
    }
}

/// Atomically replace generated keys and only the styling this feature owns.
/// `expected_keys` is the caller's already-reviewed signature; stale plans fail.
pub fn replace_generated(
    clip: &mut Clip,
    expected_keys: &[ClipKey],
    keys: Vec<ClipKey>,
    preset: String,
    canvas_style: Option<(f64, Option<crate::model::ClipMask>)>,
) -> Result<bool, crate::CommandError> {
    use crate::model::Background;
    if (is_canvas_animation(&preset) || canvas_style.is_some())
        && (has_canvas_preset(clip) || clip.extra.contains_key("canvasOwner"))
    {
        return Err(crate::CommandError::UnsafeEdit {
            reason: "Canvas Animation cannot be applied to media with an active Canvas Preset.".into(),
        });
    }
    if clip.keys != expected_keys
        || !clip.kind.is_visual()
        || keys.is_empty()
        || (!clip.keys.is_empty() && generated_ownership(clip).is_none())
        || !keys.iter().all(|k| {
            k.at.is_finite()
                && (0.0..=1.0).contains(&k.at)
                && k.value.is_finite()
                && k.ease.0.iter().all(|v| v.is_finite())
        })
        || canvas_style
            .as_ref()
            .is_some_and(|(blur, _)| !blur.is_finite() || !(0.0..=1.0).contains(blur))
    {
        return Err(crate::CommandError::UnsafeEdit {
            reason: "Animation target changed or settings are invalid".into(),
        });
    }
    let old = saved_ownership(clip).and_then(|saved| saved.style);
    let style = if let Some((blur, requested_mask)) = canvas_style {
        let mut next = old.clone().unwrap_or_else(|| GeneratedStyle {
            owns_background: true,
            owns_mask: clip.mask.is_none(),
            before_background: clip.background.clone(),
            applied_background: clip.background.clone(),
            before_mask: clip.mask.clone(),
            applied_mask: clip.mask.clone(),
        });
        // Do not claim ownership of a later independent background/mask edit.
        if next.owns_background
            && old
                .as_ref()
                .is_none_or(|old| clip.background == old.applied_background)
        {
            clip.background = Some(Background::Blur { amount: blur });
            next.applied_background = clip.background.clone();
        } else {
            next.owns_background = false;
        }
        if next.owns_mask && old.as_ref().is_none_or(|old| clip.mask == old.applied_mask) {
            clip.mask = requested_mask.map(crate::model::ClipMask::tidy);
            next.applied_mask = clip.mask.clone();
        } else {
            next.owns_mask = false;
        }
        Some(next)
    } else {
        if let Some(old) = old {
            if old.owns_background && clip.background == old.applied_background {
                clip.background = old.before_background;
            }
            if old.owns_mask && clip.mask == old.applied_mask {
                clip.mask = old.before_mask;
            }
        }
        None
    };
    let before_keys = clip.keys.clone();
    let before_record = clip.extra.get(OWNERSHIP_KEY).cloned();
    // Generated samples may be closer together than the manual key editor's
    // hit tolerance. Install the full vector, then use the model's canonical
    // clamps/sort without set_key merging distinct visible frames.
    let mut canonical = clip.clone();
    canonical.keys = keys;
    clip.keys = canonical.tidy().keys;
    let saved = GeneratedOwnership {
        version: 1,
        preset,
        keys: clip.keys.clone(),
        style,
    };
    let value = serde_json::to_value(saved).map_err(|e| crate::CommandError::UnsafeEdit {
        reason: e.to_string(),
    })?;
    let changed = before_keys != clip.keys || before_record.as_ref() != Some(&value);
    clip.extra.insert(OWNERSHIP_KEY.into(), value);
    Ok(changed)
}

/// Remove only an exactly recognized, still-owned generated animation.
///
/// None is intentionally not another `replace_generated` call: that command requires
/// nonempty keys.  Never clear manually edited keys or unrelated style channels.
pub fn clear_generated(
    clip: &mut Clip,
    expected_keys: &[ClipKey],
) -> Result<bool, crate::CommandError> {
    if !clip.kind.is_visual() || clip.keys != expected_keys {
        return Err(crate::CommandError::UnsafeEdit {
            reason: "Animation target changed before clearing".into(),
        });
    }
    let Some(owned) = generated_ownership(clip) else {
        return Err(crate::CommandError::UnsafeEdit {
            reason: "Manual or unowned animation keys are protected".into(),
        });
    };
    // Independently edited style no longer belongs to this animation.
    if let Some(style) = owned.style {
        if style.owns_background && clip.background == style.applied_background {
            clip.background = style.before_background;
        }
        if style.owns_mask && clip.mask == style.applied_mask {
            clip.mask = style.before_mask;
        }
    }
    clip.keys.clear();
    clip.extra.remove(OWNERSHIP_KEY);
    Ok(true)
}

/// The starter animations available from the native picture inspector.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MotionPreset {
    /// Increase the clip's current scale by 18 percent.
    PushIn,
    /// Move the clip right by eight percent of the frame width.
    PanRight,
    /// Raise opacity from zero to the clip's current setting.
    FadeIn,
    PushOut,
    PanLeft,
    PanUp,
    PanDown,
    FadeOut,
    RotateLeft,
    RotateRight,
    OpacityPulse,
}

/// Why a motion preset cannot be placed on this clip.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PresetError {
    /// Presets apply to image and video clips.
    NeedsPicture,
    /// The clip must have at least two visible frames.
    TooShort,
    /// Applying the preset would compete with keys on the same property.
    AlreadyKeyed,
    /// The property's value is already at the preset's limit.
    NoChange,
}

impl MotionPreset {
    /// Stable catalogue IDs; never change existing IDs because user settings depend on them.
    pub const fn id(self) -> &'static str {
        match self {
            Self::PushIn => "motion.push-in",
            Self::PanRight => "motion.pan-right",
            Self::FadeIn => "motion.fade-in",
            Self::PushOut => "motion.push-out",
            Self::PanLeft => "motion.pan-left",
            Self::PanUp => "motion.pan-up",
            Self::PanDown => "motion.pan-down",
            Self::FadeOut => "motion.fade-out",
            Self::RotateLeft => "motion.rotate-left",
            Self::RotateRight => "motion.rotate-right",
            Self::OpacityPulse => "motion.opacity-pulse",
        }
    }

    /// Build one undoable edit and return the timeline time of a frame in
    /// its range so the inspector can show the effect immediately.
    pub fn command(self, clip: &Clip, frame: f64) -> Result<(Command, f64), PresetError> {
        let (keys, preview) = self.keys(clip, frame)?;
        let commands = keys
            .into_iter()
            .map(|key| Command::SetClipKey {
                clip_id: clip.id.clone(),
                property: key.property,
                at: key.at,
                value: key.value,
                ease: key.ease,
            })
            .collect();
        Ok((Command::Batch { commands }, preview))
    }

    /// Calculate the preset's native keys before committing an edit.
    pub fn keys(self, clip: &Clip, frame: f64) -> Result<(Vec<ClipKey>, f64), PresetError> {
        self.keys_with(clip, frame, 1.0, 1.0, 1.0)
    }

    /// Shared authoring controls, baked into native editable keys. Opacity Pulse
    /// uses three visible-frame keys; the other Basic presets use two.
    pub fn keys_with(
        self,
        clip: &Clip,
        frame: f64,
        strength: f64,
        duration: f64,
        speed: f64,
    ) -> Result<(Vec<ClipKey>, f64), PresetError> {
        if !strength.is_finite()
            || !duration.is_finite()
            || !speed.is_finite()
            || !(0.1..=2.0).contains(&strength)
            || !(0.1..=1.0).contains(&duration)
            || !(0.25..=3.0).contains(&speed)
        {
            return Err(PresetError::NoChange);
        }
        if !matches!(clip.kind, ClipKind::Video | ClipKind::Image) {
            return Err(PresetError::NeedsPicture);
        }
        if !frame.is_finite() || frame <= 0.0 || !clip.duration.is_finite() {
            return Err(PresetError::TooShort);
        }
        // The clip ends just after its last visible frame on a frame-aligned
        // edit. Use a key on a visible frame rather than on that boundary.
        let last = (clip.duration / frame - 1e-9).ceil() - 1.0;
        if !last.is_finite() || last < 1.0 {
            return Err(PresetError::TooShort);
        }
        let (property, first, final_value, end_frame) = match self {
            Self::PushIn => (
                KeyProperty::Scale,
                clip.scale,
                (clip.scale * 1.18).min(ranges::MAX_SCALE),
                last,
            ),
            Self::PanRight => (
                KeyProperty::OffsetX,
                clip.offset_x,
                (clip.offset_x + 0.08).min(ranges::MAX_OFFSET),
                last,
            ),
            Self::FadeIn => (
                KeyProperty::Opacity,
                0.0,
                clip.opacity,
                // Auto Fit is the default: fade over the visible clip,
                // while explicit duration/speed still shorten the movement.
                last,
            ),
            Self::PushOut => (
                KeyProperty::Scale,
                (clip.scale * 1.18).min(ranges::MAX_SCALE),
                clip.scale,
                last,
            ),
            Self::PanLeft => (KeyProperty::OffsetX, clip.offset_x,
                (clip.offset_x - 0.08).max(-ranges::MAX_OFFSET), last),
            Self::PanUp => (KeyProperty::OffsetY, clip.offset_y,
                (clip.offset_y - 0.08).max(-ranges::MAX_OFFSET), last),
            Self::PanDown => (KeyProperty::OffsetY, clip.offset_y,
                (clip.offset_y + 0.08).min(ranges::MAX_OFFSET), last),
            Self::FadeOut => (KeyProperty::Opacity, clip.opacity, 0.0,
                (0.1 * clip.duration / frame).ceil().min(last)),
            Self::RotateLeft => (KeyProperty::Rotation, clip.rotation,
                (clip.rotation - 12.0).max(-360.0), last),
            Self::RotateRight => (KeyProperty::Rotation, clip.rotation,
                (clip.rotation + 12.0).min(360.0), last),
            Self::OpacityPulse => (KeyProperty::Opacity, clip.opacity,
                clip.opacity * 0.65, last),
        };
        let final_value = match property {
            KeyProperty::Scale => (first + (final_value - first) * strength)
                .clamp(ranges::MIN_SCALE, ranges::MAX_SCALE),
            KeyProperty::Opacity => (first + (final_value - first) * strength).clamp(0.0, 1.0),
            KeyProperty::Rotation => (first + (final_value - first) * strength).clamp(-360.0, 360.0),
            _ => (first + (final_value - first) * strength)
                .clamp(-ranges::MAX_OFFSET, ranges::MAX_OFFSET),
        };
        let end_frame = (end_frame * duration / speed).round().clamp(1.0, last);
        if clip.is_keyed(property) {
            return Err(PresetError::AlreadyKeyed);
        }
        if first == final_value {
            return Err(PresetError::NoChange);
        }
        let end = end_frame * frame / clip.duration;
        let mut stops = vec![
            (0.0, first, KeyEase::LINEAR),
            (end, final_value, KeyEase::IN_OUT),
        ];
        if self == Self::OpacityPulse {
            // A genuine down-and-up pulse needs three visible keyframes.
            if end_frame < 2.0 { return Err(PresetError::TooShort); }
            let middle = (end_frame / 2.0).round().clamp(1.0, end_frame - 1.0);
            stops = vec![
                (0.0, first, KeyEase::LINEAR),
                (middle * frame / clip.duration, final_value, KeyEase::IN_OUT),
                (end, first, KeyEase::IN_OUT),
            ];
        }
        let keys = stops.into_iter().map(|(at, value, ease)| ClipKey {
            property, at, value, ease,
        }).collect();
        let preview = clip.start + (end_frame / 2.0).round() * frame;
        Ok((keys, preview))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::NewMedia;
    use crate::model::MediaKind;
    use crate::{DocumentSettings, Editor, to_document};

    const FRAME: f64 = 1.0 / 30.0;

    fn apply_preset(clip: &mut Clip, preset: MotionPreset) -> f64 {
        let (Command::Batch { commands }, preview) = preset.command(clip, FRAME).unwrap() else {
            panic!("a preset must be one undo step");
        };
        assert_eq!(commands.len(), 2);
        for command in commands {
            let Command::SetClipKey {
                property,
                at,
                value,
                ease,
                ..
            } = command
            else {
                panic!("a preset must use existing project keys");
            };
            clip.set_key(property, at, value, ease);
        }
        preview
    }

    #[test]
    fn cross_family_replacement_keeps_one_preset_and_protects_authored_keys() {
        use crate::canvas_motion::{CanvasMotionControls, CanvasMotionPreset};
        use crate::physics_motion::{PhysicsControls, PhysicsPreset};
        for kind in [ClipKind::Image, ClipKind::Video] {
            let mut clip = Clip::blank("c1", "t1", kind, "media", 0.0, 6.0);
            let mut signatures: Vec<Vec<ClipKey>> = Vec::new();
            for family in [0, 1, 2, 1, 0, 0] {
                let base = preset_base(&clip, signatures.iter().map(Vec::as_slice)).unwrap();
                assert!(base.keys.is_empty());
                let keys = match family {
                    0 => {
                        PhysicsPreset::SpringZoom
                            .keys_with(&base, FRAME, PhysicsControls::default())
                            .unwrap()
                            .0
                    }
                    1 => CanvasMotionPreset::ALL[0]
                        .keys(&base, FRAME, CanvasMotionControls::default())
                        .unwrap(),
                    _ => MotionPreset::PushIn.keys(&base, FRAME).unwrap().0.to_vec(),
                };
                clip.keys = keys.clone();
                signatures.push(keys);
                assert!(
                    preset_base(&clip, signatures.iter().map(Vec::as_slice))
                        .unwrap()
                        .keys
                        .is_empty()
                );
            }
            // Undo to an earlier generated signature remains replaceable.
            clip.keys = signatures[0].clone();
            assert!(preset_base(&clip, signatures.iter().map(Vec::as_slice)).is_some());
            // A reload with no ownership record must not erase ambiguous keys.
            assert!(preset_base(&clip, std::iter::empty()).is_none());
            clip.keys[0].value += 0.123;
            assert!(preset_base(&clip, signatures.iter().map(Vec::as_slice)).is_none());
        }
    }

    #[test]
    fn push_and_pan_start_at_existing_framing_and_preview_inside_the_clip() {
        let mut clip = Clip::blank("c1", "t1", ClipKind::Image, "picture", 2.0, 4.0);
        clip.scale = 1.5;
        clip.offset_x = -0.2;
        let preview = apply_preset(&mut clip, MotionPreset::PushIn);
        assert!(preview > 2.0 && preview < 6.0);
        assert_eq!(clip.value_at(KeyProperty::Scale, 0.0), 1.5);
        assert!((clip.value_at(KeyProperty::Scale, 1.0) - 1.77).abs() < 1e-9);
        assert_eq!(clip.offset_x, -0.2);

        apply_preset(&mut clip, MotionPreset::PanRight);
        assert_eq!(clip.value_at(KeyProperty::OffsetX, 0.0), -0.2);
        assert!((clip.value_at(KeyProperty::OffsetX, 1.0) + 0.12).abs() < 1e-9);
        assert_eq!(clip.keys_on(KeyProperty::Scale).count(), 2);
    }

    #[test]
    fn fade_holds_existing_opacity_and_preserves_existing_keys() {
        let mut clip = Clip::blank("c1", "t1", ClipKind::Video, "picture", 0.0, 10.0);
        clip.opacity = 0.6;
        let preview = apply_preset(&mut clip, MotionPreset::FadeIn);
        assert!((preview - 5.0).abs() <= FRAME);
        assert_eq!(clip.value_at(KeyProperty::Opacity, 0.0), 0.0);
        assert!(clip.value_at(KeyProperty::Opacity, 0.05) > 0.0);
        assert!(clip.value_at(KeyProperty::Opacity, 0.1) < 0.6);
        assert_eq!(clip.value_at(KeyProperty::Opacity, 1.0), 0.6);
        assert_eq!(
            MotionPreset::FadeIn.command(&clip, FRAME).unwrap_err(),
            PresetError::AlreadyKeyed,
        );
    }

    #[test]
    fn expanded_basic_presets_are_native_bounded_and_frame_aligned() {
        let clip = Clip::blank("c1", "t1", ClipKind::Video, "picture", 0.0, 3.0);
        for preset in [
            MotionPreset::PushOut, MotionPreset::PanLeft, MotionPreset::PanUp,
            MotionPreset::PanDown, MotionPreset::FadeOut, MotionPreset::RotateLeft,
            MotionPreset::RotateRight, MotionPreset::OpacityPulse,
        ] {
            let (keys, _) = preset.keys(&clip, FRAME).unwrap();
            assert!(keys.len() >= 2);
            assert!(keys.windows(2).all(|p| p[0].at < p[1].at));
            assert!(keys.iter().all(|k| k.value.is_finite() && k.at < 1.0));
            assert!(keys.iter().all(|k| k.property == keys[0].property));
        }
        let pulse = MotionPreset::OpacityPulse.keys(&clip, FRAME).unwrap().0;
        assert_eq!(pulse.len(), 3);
        assert!((pulse[0].value - pulse[2].value).abs() < 1e-10);
        assert!(pulse[1].value < pulse[0].value);
        let mut authored = clip.clone();
        authored.set_key(KeyProperty::Rotation, 0.2, 10.0, KeyEase::LINEAR);
        assert_eq!(MotionPreset::RotateRight.keys(&authored, FRAME).unwrap_err(),
            PresetError::AlreadyKeyed);
    }

    #[test]
    fn preset_rejects_one_frame_and_audio_clips() {
        let short = Clip::blank("c1", "t1", ClipKind::Image, "still", 0.0, FRAME);
        assert_eq!(
            MotionPreset::PushIn.command(&short, FRAME).unwrap_err(),
            PresetError::TooShort,
        );
        let audio = Clip::blank("c2", "t1", ClipKind::Audio, "voice", 0.0, 2.0);
        assert_eq!(
            MotionPreset::FadeIn.command(&audio, FRAME).unwrap_err(),
            PresetError::NeedsPicture,
        );
    }

    #[test]
    fn preset_survives_document_round_trip_and_undo_removes_both_keys() {
        let mut editor = Editor::new();
        let media_id = editor
            .apply(Command::AddMedia {
                item: NewMedia {
                    path: "/clip.mp4".to_owned(),
                    name: "clip.mp4".to_owned(),
                    duration: Some(2.0),
                    kind: MediaKind::Video,
                    width: Some(1920),
                    height: Some(1080),
                    frame_rate: Some(30.0),
                    frame_rate_fraction: Some("30/1".to_owned()),
                    video_codec: Some("h264".to_owned()),
                    audio_codec: None,
                    has_audio: false,
                    audio_tracks: Vec::new(),
                    origin: None,
                    color_space: Default::default(),
                },
            })
            .unwrap()
            .created_id
            .unwrap();
        let track_id = editor.project().active().tracks[0].id.clone();
        let clip_id = editor
            .apply(Command::AddClip {
                media_id,
                track_id,
                start: 0.0,
                ripple: false,
            })
            .unwrap()
            .created_id
            .unwrap();
        let clip = editor.project().active().clip(&clip_id).unwrap();
        let (command, _) = MotionPreset::PushIn.command(clip, FRAME).unwrap();
        editor.apply(command).unwrap();
        let settings = DocumentSettings {
            name: "Motion fixture".to_owned(),
            width: 1920,
            height: 1080,
            rate_num: 30,
            rate_den: 1,
        };
        let doc = to_document(&settings, editor.project());
        let restored = Editor::from_document(&doc).unwrap();
        let saved = restored.project().active().clip(&clip_id).unwrap();
        assert_eq!(saved.keys_on(KeyProperty::Scale).count(), 2);
        assert_eq!(saved.value_at(KeyProperty::Scale, 1.0), 1.18);
        assert!(editor.undo());
        assert!(
            !editor
                .project()
                .active()
                .clip(&clip_id)
                .unwrap()
                .is_keyed(KeyProperty::Scale)
        );
    }
    fn owned_fixture() -> Editor {
        use crate::model::{MediaItem, MediaKind};
        let mut editor = Editor::new();
        editor.project_mut().media.push(MediaItem {
            id: "m1".into(),
            path: "/fixture.mp4".into(),
            name: "fixture".into(),
            kind: MediaKind::Video,
            width: Some(1920),
            height: Some(1080),
            duration: Some(10.0),
            ..Default::default()
        });
        let mut clip = Clip::blank("c1", "T1", ClipKind::Video, "picture", 0.0, 10.0);
        clip.media_id = "m1".into();
        editor
            .project_mut()
            .active_mut()
            .clips
            .push(std::sync::Arc::new(clip));
        editor
    }

    fn generated_command(
        editor: &Editor,
        id: &str,
        canvas: bool,
        blur: f64,
        radius: f64,
    ) -> Command {
        let clip = editor.project().active().clip(id).unwrap();
        let base = preset_base(clip, std::iter::empty()).unwrap();
        let (preset, keys, canvas_style) = if canvas {
            use crate::canvas_motion::{CanvasMotionControls, CanvasMotionPreset, rounded_mask};
            let preset = CanvasMotionPreset::ALL[0];
            (
                preset.id(),
                preset
                    .keys(&base, FRAME, CanvasMotionControls::default())
                    .unwrap(),
                Some((
                    blur,
                    (radius > 0.0).then(|| rounded_mask(1920, 1080, radius).unwrap()),
                )),
            )
        } else {
            (
                "motion.push-in",
                MotionPreset::PushIn.keys(&base, FRAME).unwrap().0.to_vec(),
                None,
            )
        };
        Command::ApplyGeneratedMotion {
            clip_id: id.into(),
            expected_keys: clip.keys.clone(),
            keys,
            preset: preset.into(),
            canvas_style,
        }
    }

    fn reopen(editor: &Editor) -> Editor {
        let settings = DocumentSettings {
            name: "ownership".into(),
            width: 1920,
            height: 1080,
            rate_num: 30,
            rate_den: 1,
        };
        // Include JSON text IO, rather than only cloning in-memory Values.
        let text = serde_json::to_string(&to_document(&settings, editor.project())).unwrap();
        Editor::from_document(&serde_json::from_str(&text).unwrap()).unwrap()
    }

    #[test]
    fn generated_ownership_survives_reopen_cross_family_replacement_and_undo() {
        let mut editor = owned_fixture();
        for canvas in [false, true, true, false] {
            editor
                .apply(generated_command(&editor, "c1", canvas, 0.5, 0.1))
                .unwrap();
            editor = reopen(&editor);
            let clip = editor.project().active().clip("c1").unwrap();
            assert!(generated_ownership(clip).is_some());
            assert!(preset_base(clip, std::iter::empty()).is_some());
        }
        editor
            .apply(generated_command(&editor, "c1", true, 0.2, 0.05))
            .unwrap();
        let x = editor.project().active().clip("c1").unwrap().clone();
        editor
            .apply(generated_command(&editor, "c1", true, 0.8, 0.1))
            .unwrap();
        assert!(editor.undo());
        assert_eq!(editor.project().active().clip("c1").unwrap(), &x);
        assert!(editor.redo());
        assert!(editor.undo());
        editor
            .apply(generated_command(&editor, "c1", false, 0.0, 0.0))
            .unwrap();
        assert!(generated_ownership(editor.project().active().clip("c1").unwrap()).is_some());
    }

    #[test]
    fn explicit_none_clears_owned_keys_and_canvas_style_with_undo_redo() {
        let mut editor = owned_fixture();
        let original = editor.project().active().clip("c1").unwrap().clone();
        editor.apply(generated_command(&editor, "c1", true, 0.6, 0.1)).unwrap();
        let generated = editor.project().active().clip("c1").unwrap().clone();
        assert!(!generated.keys.is_empty());
        assert!(generated_ownership(&generated).is_some());

        editor.apply(Command::ClearGeneratedMotion {
            clip_id: "c1".into(),
            expected_keys: generated.keys.clone(),
        }).unwrap();
        let cleared = editor.project().active().clip("c1").unwrap();
        assert!(cleared.keys.is_empty());
        assert_eq!(cleared.background, original.background);
        assert_eq!(cleared.mask, original.mask);
        assert!(cleared.extra.get(OWNERSHIP_KEY).is_none());

        assert!(editor.undo());
        assert_eq!(editor.project().active().clip("c1").unwrap(), &generated);
        assert!(editor.redo());
        assert!(editor.project().active().clip("c1").unwrap().keys.is_empty());
    }

    #[test]
    fn explicit_none_rejects_unowned_and_manually_edited_keys() {
        let mut editor = owned_fixture();
        assert!(editor.apply(Command::ClearGeneratedMotion {
            clip_id: "c1".into(), expected_keys: Vec::new(),
        }).is_err());

        editor.apply(generated_command(&editor, "c1", false, 0.0, 0.0)).unwrap();
        editor.apply(Command::SetClipKey {
            clip_id: "c1".into(),
            property: KeyProperty::Scale,
            at: 0.5,
            value: 1.25,
            ease: KeyEase::default(),
        }).unwrap();
        let authored = editor.project().active().clip("c1").unwrap().clone();
        assert!(generated_ownership(&authored).is_none());
        assert!(editor.apply(Command::ClearGeneratedMotion {
            clip_id: "c1".into(), expected_keys: authored.keys.clone(),
        }).is_err());
        assert_eq!(editor.project().active().clip("c1").unwrap(), &authored);
    }

    #[test]
    fn style_only_updates_change_blur_and_corners_without_stacking_keys() {
        use crate::model::Background;
        let mut editor = owned_fixture();
        let mut previous_keys = None;
        for (blur, radius) in [(0.2, 0.05), (0.8, 0.12), (0.8, 0.0)] {
            assert!(
                editor
                    .apply(generated_command(&editor, "c1", true, blur, radius))
                    .unwrap()
                    .applied
            );
            let clip = editor.project().active().clip("c1").unwrap();
            assert_eq!(clip.background, Some(Background::Blur { amount: blur }));
            assert_eq!(
                clip.mask,
                (radius > 0.0)
                    .then(|| crate::canvas_motion::rounded_mask(1920, 1080, radius).unwrap())
            );
            if let Some(keys) = &previous_keys {
                assert_eq!(&clip.keys, keys);
            }
            previous_keys = Some(clip.keys.clone());
            editor = reopen(&editor);
        }
        // Rebuild once after JSON IO; float parsing may change a last bit.
        editor
            .apply(generated_command(&editor, "c1", true, 0.8, 0.0))
            .unwrap();
        assert!(
            !editor
                .apply(generated_command(&editor, "c1", true, 0.8, 0.0))
                .unwrap()
                .applied
        );
        editor
            .apply(generated_command(&editor, "c1", false, 0.0, 0.0))
            .unwrap();
        let clip = editor.project().active().clip("c1").unwrap();
        assert_eq!(clip.background, None);
        assert_eq!(clip.mask, None);
    }

    #[test]
    fn animation_style_replacement_preserves_authored_masks_and_background_edits() {
        use crate::commands::ClipPatch;
        use crate::model::{Background, ClipMask, MaskShape};
        let mut editor = owned_fixture();
        let authored = Some(ClipMask::of(MaskShape::Circle));
        editor
            .apply(Command::UpdateClip {
                clip_id: "c1".into(),
                patch: ClipPatch {
                    mask: Some(authored.clone()),
                    ..Default::default()
                },
            })
            .unwrap();
        editor
            .apply(generated_command(&editor, "c1", true, 0.4, 0.1))
            .unwrap();
        assert_eq!(editor.project().active().clip("c1").unwrap().mask, authored);
        let background = Some(Background::Color {
            color: "#abcdef".into(),
        });
        editor
            .apply(Command::UpdateClip {
                clip_id: "c1".into(),
                patch: ClipPatch {
                    background: Some(background.clone()),
                    ..Default::default()
                },
            })
            .unwrap();
        editor
            .apply(generated_command(&editor, "c1", true, 0.8, 0.15))
            .unwrap();
        editor
            .apply(generated_command(&editor, "c1", false, 0.0, 0.0))
            .unwrap();
        let clip = editor.project().active().clip("c1").unwrap();
        assert_eq!(clip.mask, authored);
        assert_eq!(clip.background, background);
    }

    #[test]
    fn trim_and_split_keep_owned_motion_replaceable_but_not_manual_keys() {
        use crate::commands::TrimEdge;
        let mut editor = owned_fixture();
        editor
            .apply(generated_command(&editor, "c1", true, 0.4, 0.1))
            .unwrap();
        editor
            .apply(Command::TrimClip {
                clip_id: "c1".into(),
                edge: TrimEdge::Start,
                delta: 1.0,
                ripple: false,
            })
            .unwrap();
        let tail = editor
            .apply(Command::SplitClips {
                clip_ids: vec!["c1".into()],
                time: 4.0,
            })
            .unwrap()
            .created_id
            .unwrap();
        editor = reopen(&editor);
        for id in ["c1", tail.as_str()] {
            assert!(generated_ownership(editor.project().active().clip(id).unwrap()).is_some());
            editor
                .apply(generated_command(&editor, id, false, 0.0, 0.0))
                .unwrap();
        }
        editor
            .apply(Command::SetClipKey {
                clip_id: tail.clone(),
                property: KeyProperty::Rotation,
                at: 0.5,
                value: 9.0,
                ease: KeyEase::LINEAR,
            })
            .unwrap();
        editor
            .apply(Command::TrimClip {
                clip_id: tail.clone(),
                edge: TrimEdge::End,
                delta: -0.5,
                ripple: false,
            })
            .unwrap();
        let clip = editor.project().active().clip(&tail).unwrap();
        assert!(preset_base(clip, std::iter::empty()).is_none());
        assert!(generated_ownership(clip).is_none());
    }

    #[test]
    fn stale_generated_plan_rolls_back_the_whole_batch() {
        use crate::commands::ClipPatch;
        let mut editor = owned_fixture();
        let stale = generated_command(&editor, "c1", false, 0.0, 0.0);
        editor
            .apply(Command::SetClipKey {
                clip_id: "c1".into(),
                property: KeyProperty::Rotation,
                at: 0.5,
                value: 3.0,
                ease: KeyEase::LINEAR,
            })
            .unwrap();
        let before = editor.project().clone();
        assert!(
            editor
                .apply(Command::Batch {
                    commands: vec![
                        Command::UpdateClip {
                            clip_id: "c1".into(),
                            patch: ClipPatch {
                                name: Some("should roll back".into()),
                                ..Default::default()
                            }
                        },
                        stale
                    ]
                })
                .is_err()
        );
        assert_eq!(editor.project(), &before);
    }
    #[test]
    fn dense_generated_keys_survive_application_and_document_io() {
        let mut editor = owned_fixture();
        editor
            .project_mut()
            .active_mut()
            .clip_mut("c1")
            .unwrap()
            .duration = 600.0;
        use crate::canvas_motion::{CanvasMotionControls, CanvasMotionPreset};
        let clip = editor.project().active().clip("c1").unwrap();
        let keys = CanvasMotionPreset::ALL[0]
            .keys(
                clip,
                FRAME,
                CanvasMotionControls {
                    // This legacy dense-key scenario explicitly exercises a
                    // short manual motion on a much longer timeline clip.
                    auto_fit: false,
                    duration_seconds: 1.0,
                    ..Default::default()
                },
            )
            .unwrap();
        let count = keys.len();
        assert!(
            keys.iter()
                .any(|k| k.at > 0.0 && k.at < crate::model::KEY_EPSILON)
        );
        editor
            .apply(Command::ApplyGeneratedMotion {
                clip_id: "c1".into(),
                expected_keys: vec![],
                keys,
                preset: CanvasMotionPreset::ALL[0].id().into(),
                canvas_style: None,
            })
            .unwrap();
        assert_eq!(
            editor.project().active().clip("c1").unwrap().keys.len(),
            count
        );
        editor = reopen(&editor);
        let clip = editor.project().active().clip("c1").unwrap();
        assert_eq!(clip.keys.len(), count);
        assert!(generated_ownership(clip).is_some());
        if let Ok(directory) = std::env::var("CONCAT_CANVAS_FIXTURE_DIR") {
            let settings = DocumentSettings { name:"Motion worker contract".into(),width:1920,height:1080,rate_num:30,rate_den:1 };
            let samples: Vec<_> = (0..=30).map(|frame| {
                let at = f64::from(frame) * FRAME / clip.duration;
                serde_json::json!({"frame":frame,"scale":clip.value_at(KeyProperty::Scale,at),
                    "pan_x":clip.value_at(KeyProperty::OffsetX,at),"pan_y":clip.value_at(KeyProperty::OffsetY,at),
                    "opacity":clip.value_at(KeyProperty::Opacity,at)})
            }).collect();
            let contract = serde_json::json!({"document":to_document(&settings,editor.project()),"samples":samples});
            std::fs::create_dir_all(&directory).unwrap();
            std::fs::write(std::path::Path::new(&directory).join("generated-motion-contract.json"),
                serde_json::to_vec_pretty(&contract).unwrap()).unwrap();
        }
    }

    #[test]
    fn explicitly_matching_manual_signature_still_cannot_claim_authored_keys() {
        let mut editor = owned_fixture();
        editor
            .apply(Command::SetClipKey {
                clip_id: "c1".into(),
                property: KeyProperty::Rotation,
                at: 0.3,
                value: 8.0,
                ease: KeyEase::LINEAR,
            })
            .unwrap();
        let clip = editor.project().active().clip("c1").unwrap();
        let keys = clip.keys.clone();
        let before = editor.project().clone();
        assert!(
            editor
                .apply(Command::ApplyGeneratedMotion {
                    clip_id: "c1".into(),
                    expected_keys: keys.clone(),
                    keys,
                    preset: "motion.push-in".into(),
                    canvas_style: None
                })
                .is_err()
        );
        assert_eq!(editor.project(), &before);
    }
}
