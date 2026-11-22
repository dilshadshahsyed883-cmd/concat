// SPDX-License-Identifier: AGPL-3.0-or-later
//! Canvas motion is baked into the same editable native keys used by both renderers.
//! Backdrop and caption/badge layers are intentionally independent of these keys.
#![allow(missing_docs)]
use crate::Command;
use crate::model::{Background, Clip, ClipKey, KeyEase, KeyProperty};
use crate::motion_preset::PresetError;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CanvasMotionPreset {
    TopLeft,
    TopRight,
    BottomLeft,
    BottomRight,
    CenterPop,
    ZoomLeft,
    ZoomCenter,
    ZoomRight,
    ReverseCorner,
    SlideLeft,
    SlideRight,
    SlideTop,
    SlideBottom,
    Diagonal,
    SpringSettle,
    Documentary,
    FadeScale,
    Alternating,
    BreathingLoop,
    InHoldOut,
    FocusJourney,
}
impl CanvasMotionPreset {
    pub const ALL: [Self; 21] = [
        Self::TopLeft,
        Self::TopRight,
        Self::BottomLeft,
        Self::BottomRight,
        Self::CenterPop,
        Self::ZoomLeft,
        Self::ZoomCenter,
        Self::ZoomRight,
        Self::ReverseCorner,
        Self::SlideLeft,
        Self::SlideRight,
        Self::SlideTop,
        Self::SlideBottom,
        Self::Diagonal,
        Self::SpringSettle,
        Self::Documentary,
        Self::FadeScale,
        Self::Alternating,
        Self::BreathingLoop,
        Self::InHoldOut,
        Self::FocusJourney,
    ];
    pub fn from_index(index: i32) -> Option<Self> {
        usize::try_from(index)
            .ok()
            .and_then(|i| Self::ALL.get(i).copied())
    }
    pub const fn id(self) -> &'static str {
        match self {
            Self::TopLeft => "canvas-motion.top-left",
            Self::TopRight => "canvas-motion.top-right",
            Self::BottomLeft => "canvas-motion.bottom-left",
            Self::BottomRight => "canvas-motion.bottom-right",
            Self::CenterPop => "canvas-motion.center-pop",
            Self::ZoomLeft => "canvas-motion.zoom-left",
            Self::ZoomCenter => "canvas-motion.zoom-center",
            Self::ZoomRight => "canvas-motion.zoom-right",
            Self::ReverseCorner => "canvas-motion.reverse-corner",
            Self::SlideLeft => "canvas-motion.slide-left",
            Self::SlideRight => "canvas-motion.slide-right",
            Self::SlideTop => "canvas-motion.slide-top",
            Self::SlideBottom => "canvas-motion.slide-bottom",
            Self::Diagonal => "canvas-motion.diagonal",
            Self::SpringSettle => "canvas-motion.spring-settle",
            Self::Documentary => "canvas-motion.documentary",
            Self::FadeScale => "canvas-motion.fade-scale",
            Self::Alternating => "canvas-motion.alternating",
            Self::BreathingLoop => "canvas-motion.breathing-loop",
            Self::InHoldOut => "canvas-motion.in-hold-out",
            Self::FocusJourney => "canvas-motion.focus-journey",
        }
    }
    pub fn anchors(self, seed: u64) -> ([f64; 2], [f64; 2]) {
        match self {
            Self::TopLeft | Self::ReverseCorner => ([-0.32, -0.32], [0.0, 0.0]),
            Self::TopRight => ([0.32, -0.32], [0.0, 0.0]),
            Self::BottomLeft => ([-0.32, 0.32], [0.0, 0.0]),
            Self::BottomRight => ([0.32, 0.32], [0.0, 0.0]),
            Self::ZoomLeft => ([-0.32, 0.0], [-0.04, 0.0]),
            Self::ZoomRight => ([0.32, 0.0], [0.04, 0.0]),
            Self::SlideLeft => ([-0.38, 0.0], [0.0, 0.0]),
            Self::SlideRight => ([0.38, 0.0], [0.0, 0.0]),
            Self::SlideTop => ([0.0, -0.38], [0.0, 0.0]),
            Self::SlideBottom => ([0.0, 0.38], [0.0, 0.0]),
            Self::Diagonal => ([-0.32, -0.32], [0.04, 0.04]),
            Self::Documentary => ([-0.03, 0.0], [0.03, 0.0]),
            Self::FocusJourney => ([-0.32, 0.0], [0.32, 0.0]),
            Self::Alternating => {
                if seed % 2 == 0 {
                    ([-0.32, -0.32], [0.0, 0.0])
                } else {
                    ([0.32, 0.32], [0.0, 0.0])
                }
            }
            _ => ([0.0, 0.0], [0.0, 0.0]),
        }
    }
    pub fn keys(
        self,
        clip: &Clip,
        frame: f64,
        controls: CanvasMotionControls,
    ) -> Result<Vec<ClipKey>, PresetError> {
        if !clip.kind.is_visual() {
            return Err(PresetError::NeedsPicture);
        }
        if !clip.duration.is_finite()
            || !frame.is_finite()
            || frame <= 0.0
            || clip.duration < frame * 2.0
        {
            return Err(PresetError::TooShort);
        }
        if [
            KeyProperty::Scale,
            KeyProperty::OffsetX,
            KeyProperty::OffsetY,
            KeyProperty::Opacity,
        ]
        .iter()
        .any(|p| clip.is_keyed(*p))
        {
            return Err(PresetError::AlreadyKeyed);
        }
        if controls.delay_seconds >= clip.duration - frame || !controls.valid() {
            return Err(PresetError::NoChange);
        }
        let last = (clip.duration / frame - 1e-9).ceil() - 1.0;
        if !last.is_finite() || last > i32::MAX as f64 {
            return Err(PresetError::TooShort);
        }
        let (mut from, mut to) = self.anchors(controls.seed);
        if let Some(anchor) = controls.start_anchor {
            from = anchor;
        }
        if let Some(anchor) = controls.end_anchor {
            to = anchor;
        }
        // A breathing zoom stays centered; user-authored path controls are
        // reserved for motion presets that actually travel across the frame.
        if self == Self::BreathingLoop {
            from = [0.0, 0.0];
            to = [0.0, 0.0];
        }
        let delay = (controls.delay_seconds / frame).round().min(last - 1.0);
        if self == Self::InHoldOut && last - delay < 2.0 {
            return Err(PresetError::TooShort);
        }
        let end = if self == Self::InHoldOut {
            (delay + (controls.entry_seconds / controls.speed / frame)
                .round().max(1.0)).min(last - 1.0)
        } else {
            (delay + (controls.duration_seconds / controls.speed / frame)
                .round().max(1.0)).min(last)
        };
        let (exit_start, exit_end) = if self == Self::InHoldOut {
            let exit_frames = (controls.exit_seconds / controls.speed / frame)
                .round().max(1.0).min(last - end);
            // Hold = 0 means automatic: fill the rest of the clip until exit.
            let hold_frames = if controls.hold_seconds == 0.0 {
                last - end - exit_frames
            } else {
                (controls.hold_seconds / controls.speed / frame)
                    .round().max(0.0).min(last - end - exit_frames)
            };
            let start = end + hold_frames;
            (start, start + exit_frames)
        } else {
            (last, last)
        };
        // Periodic motion needs denser samples than single-shot entrances,
        // especially on longer clips. Every key remains a visible source frame.
        let samples = if self == Self::BreathingLoop {
            (last as usize).clamp(64, 240)
        } else {
            64
        };
        let mut frames: Vec<f64> = if self == Self::BreathingLoop {
            (0..=samples).map(|i| (i as f64 * last / samples as f64).round()).collect()
        } else {
            // Sample each moving interval independently, including the exit.
            // A long hold or clip tail must not consume the motion's samples.
            let mut frames = Vec::new();
            for (a, b) in [(delay, end), (exit_start, exit_end)] {
                let steps = ((b - a) as usize).min(samples);
                if steps > 0 {
                    frames.extend((0..=steps).map(|i|
                        (a + i as f64 * (b - a) / steps as f64).round()));
                }
            }
            frames
        };
        frames.extend([0.0, delay, ((delay + end) / 2.0).round(), end, exit_start, exit_end, last]);
        frames.sort_by(f64::total_cmp);
        frames.dedup();
        let mut keys = Vec::with_capacity(frames.len() * 4);
        for f in frames {
            let u = ((f - delay) / (end - delay)).clamp(0.0, 1.0);
            let reversed = controls.reverse ^ (self == Self::ReverseCorner);
            let p = if reversed { 1.0 - u } else { u };
            let eased = match controls.easing {
                MotionEasing::Linear => p,
                MotionEasing::Smooth => p * p * (3.0 - 2.0 * p),
            };
            let mut progress = eased;
            if self == Self::SpringSettle {
                let damping = (0.5 / controls.intensity).clamp(0.2, 0.95);
                let w = 14.0;
                let wd = w * (1.0 - damping * damping).sqrt();
                progress = if p == 0.0 {
                    0.0
                } else if p == 1.0 {
                    1.0
                } else {
                    1.0 - (-damping * w * p).exp()
                        * ((wd * p).cos()
                            + damping / (1.0 - damping * damping).sqrt() * (wd * p).sin())
                };
            }
            let (start_scale, end_scale) = if self == Self::Documentary {
                (0.88, 0.96)
            } else {
                (controls.start_scale, controls.end_scale)
            };
            let scale = if self == Self::BreathingLoop {
                // A whole number of zoom cycles guarantees the same framing
                // on the first and last visible frame; no loop-boundary jump.
                let active_frames = (last - delay).max(1.0);
                let ideal_cycles = active_frames * frame * controls.speed
                    / controls.duration_seconds;
                let cycles = ideal_cycles.round().clamp(
                    1.0, (samples as f64 / 12.0).max(1.0)
                );
                let phase = ((f - delay) / active_frames).clamp(0.0, 1.0);
                let rise = 0.5 * (1.0 - (std::f64::consts::TAU * cycles * phase).cos());
                (end_scale + (1.0 - end_scale).min(0.05 * controls.strength) * rise)
                    .clamp(0.05, 1.0)
            } else {
                (end_scale
                    + (start_scale - end_scale) * (1.0 - progress) * controls.strength)
                    .clamp(0.05, 1.0)
            };
            let (mut x, mut y) = if self == Self::BreathingLoop {
                (from[0], from[1])
            } else if self == Self::FocusJourney {
                // Two independent smooth legs with a fixed pause at the
                // editable middle anchor. Reverse follows the same three
                // checkpoints in the opposite direction.
                let hold = (controls.focus_hold_seconds * controls.speed
                    / controls.duration_seconds).clamp(0.0, 0.8);
                let leg = (1.0 - hold) / 2.0;
                let (a, b, t) = if p < leg {
                    (from, controls.focus_middle, (p / leg).clamp(0.0, 1.0))
                } else if p <= leg + hold {
                    (controls.focus_middle, controls.focus_middle, 0.0)
                } else {
                    (controls.focus_middle, to,
                        ((p - leg - hold) / leg).clamp(0.0, 1.0))
                };
                let q = match controls.easing {
                    MotionEasing::Linear => t,
                    MotionEasing::Smooth => t * t * (3.0 - 2.0 * t),
                };
                (a[0] + (b[0] - a[0]) * q, a[1] + (b[1] - a[1]) * q)
            } else {
                (
                    from[0] + (to[0] - from[0]) * eased,
                    from[1] + (to[1] - from[1]) * eased,
                )
            };
            // Conservative bounds also fit portrait sources, whose fitted width is smaller.
            let room = (1.0 - scale) / 2.0;
            x = x.clamp(-room, room);
            y = y.clamp(-room, room);
            let opacity = if self == Self::InHoldOut {
                let fade_in = if controls.reverse { 1.0 - eased } else { eased };
                let exit_u = ((f - exit_start) / (exit_end - exit_start))
                    .clamp(0.0, 1.0);
                let fade_out = match controls.easing {
                    MotionEasing::Linear => exit_u,
                    MotionEasing::Smooth => exit_u * exit_u * (3.0 - 2.0 * exit_u),
                };
                clip.opacity * fade_in * (1.0 - fade_out)
            } else {
                clip.opacity * if self == Self::FadeScale { eased } else { 1.0 }
            };
            for (property, value) in [
                (KeyProperty::Scale, scale),
                (KeyProperty::OffsetX, x),
                (KeyProperty::OffsetY, y),
                (KeyProperty::Opacity, opacity),
            ] {
                keys.push(ClipKey {
                    property,
                    at: f * frame / clip.duration,
                    value,
                    ease: KeyEase::LINEAR,
                });
            }
        }
        // Canonical native ordering makes saved generated signatures rerollable.
        let mut canonical = clip.clone();
        canonical.keys = keys;
        canonical.sort_keys();
        Ok(canonical.keys)
    }
    pub fn command(
        self,
        clip: &Clip,
        frame: f64,
        controls: CanvasMotionControls,
    ) -> Result<Command, PresetError> {
        let keys = self.keys(clip, frame, controls)?;
        let mut commands: Vec<_> = keys
            .into_iter()
            .map(|k| Command::SetClipKey {
                clip_id: clip.id.clone(),
                property: k.property,
                at: k.at,
                value: k.value,
                ease: k.ease,
            })
            .collect();
        commands.push(Command::UpdateClip {
            clip_id: clip.id.clone(),
            patch: crate::commands::ClipPatch {
                background: Some(Some(Background::Blur {
                    amount: controls.blur,
                })),
                ..Default::default()
            },
        });
        Ok(Command::Batch { commands })
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum MotionEasing {
    Linear,
    Smooth,
}
#[derive(Clone, Copy, Debug, serde::Serialize, serde::Deserialize)]
pub struct CanvasMotionControls {
    pub start_scale: f64,
    pub end_scale: f64,
    pub duration_seconds: f64,
    pub entry_seconds: f64,
    /// Zero means automatically hold until exit begins near the clip end.
    pub hold_seconds: f64,
    pub exit_seconds: f64,
    pub focus_middle: [f64; 2],
    pub focus_hold_seconds: f64,
    pub delay_seconds: f64,
    pub speed: f64,
    pub strength: f64,
    pub intensity: f64,
    pub blur: f64,
    pub reverse: bool,
    pub start_anchor: Option<[f64; 2]>,
    pub end_anchor: Option<[f64; 2]>,
    pub easing: MotionEasing,
    pub seed: u64,
}
impl Default for CanvasMotionControls {
    fn default() -> Self {
        Self {
            start_scale: 0.30,
            end_scale: 0.92,
            duration_seconds: 3.0,
            entry_seconds: 0.5,
            hold_seconds: 0.0,
            exit_seconds: 0.5,
            focus_middle: [0.0, 0.0],
            focus_hold_seconds: 0.5,
            delay_seconds: 0.0,
            speed: 1.0,
            strength: 1.0,
            intensity: 1.0,
            blur: 0.65,
            reverse: false,
            start_anchor: None,
            end_anchor: None,
            easing: MotionEasing::Smooth,
            seed: 0,
        }
    }
}
impl CanvasMotionControls {
    pub fn valid(self) -> bool {
        [self.start_scale, self.end_scale]
            .iter()
            .all(|v| v.is_finite() && (0.05..=1.0).contains(v))
            && self.duration_seconds.is_finite()
            && self.duration_seconds > 0.0
            && self.duration_seconds <= 3600.0
            && [self.entry_seconds, self.exit_seconds].iter()
                .all(|v| v.is_finite() && *v > 0.0 && *v <= 3600.0)
            && self.hold_seconds.is_finite()
            && (0.0..=3600.0).contains(&self.hold_seconds)
            && self.focus_hold_seconds.is_finite()
            && (0.0..=3600.0).contains(&self.focus_hold_seconds)
            && self.focus_middle.into_iter()
                .all(|v| v.is_finite() && (-0.45..=0.45).contains(&v))
            && self.delay_seconds.is_finite()
            && self.delay_seconds >= 0.0
            && self.delay_seconds <= 3600.0
            && self.speed.is_finite()
            && (0.25..=3.0).contains(&self.speed)
            && self.strength.is_finite()
            && (0.1..=2.0).contains(&self.strength)
            && self.intensity.is_finite()
            && (0.2..=2.0).contains(&self.intensity)
            && self.blur.is_finite()
            && (0.0..=1.0).contains(&self.blur)
            && [self.start_anchor, self.end_anchor]
                .into_iter()
                .flatten()
                .flatten()
                .all(|v| v.is_finite() && (-0.45..=0.45).contains(&v))
    }
}

/// Full-source rounded rectangle in the native mask's shorter-side units.
/// Very wide/tall sources exceeding the native mask limits remain unmasked.
pub fn rounded_mask(width: u32, height: u32, radius: f64) -> Option<crate::model::ClipMask> {
    use crate::model::{ClipMask, MaskShape};
    if width == 0 || height == 0 || !radius.is_finite() || !(0.0..=0.2).contains(&radius) {
        return None;
    }
    let short = f64::from(width.min(height));
    let w = f64::from(width) / short;
    let h = f64::from(height) / short;
    if w > ClipMask::MAX_SIZE || h > ClipMask::MAX_SIZE {
        return None;
    }
    let mut mask = ClipMask::of(MaskShape::Rectangle);
    mask.width = w;
    mask.height = h;
    mask.roundness = (2.0 * radius).clamp(0.0, 1.0);
    mask.feather = 0.0;
    Some(mask)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{ClipKind, Project};
    use crate::{DocumentSettings, Editor};
    use std::sync::Arc;
    #[test]
    fn principal_reference_has_three_checkpoints_and_safe_corners() {
        let c = Clip::blank("c1", "T1", ClipKind::Video, "video", 0.0, 4.0);
        let keys = CanvasMotionPreset::TopLeft
            .keys(&c, 1.0 / 30.0, CanvasMotionControls::default())
            .unwrap();
        let mut after = c.clone();
        for k in keys {
            after.set_key(k.property, k.at, k.value, k.ease);
        }
        assert!((after.value_at(KeyProperty::Scale, 0.0) - 0.3).abs() < 1e-8);
        assert!((after.value_at(KeyProperty::OffsetX, 0.0) + 0.32).abs() < 1e-8);
        assert!(after.value_at(KeyProperty::Scale, 0.375) > 0.5);
        assert!((after.value_at(KeyProperty::Scale, 0.75) - 0.92).abs() < 1e-8);
        assert_eq!(after.value_at(KeyProperty::OffsetX, 0.75), 0.0);
        for i in 0..120 {
            let t = i as f64 / 120.0;
            let scale = after.value_at(KeyProperty::Scale, t);
            for p in [KeyProperty::OffsetX, KeyProperty::OffsetY] {
                assert!(after.value_at(p, t).abs() + scale / 2.0 <= 0.500001);
            }
        }
    }
    #[test]
    fn all_variants_are_native_deterministic_and_protect_authored_keys() {
        for kind in [ClipKind::Image, ClipKind::Video] {
            for preset in CanvasMotionPreset::ALL {
                let mut c = Clip::blank("c1", "T1", kind, "source", 2.0, 8.0);
                let keys = preset
                    .keys(&c, 1.0 / 30.0, CanvasMotionControls::default())
                    .unwrap();
                assert_eq!(
                    keys,
                    preset
                        .keys(&c, 1.0 / 30.0, CanvasMotionControls::default())
                        .unwrap()
                );
                assert!(
                    keys.iter()
                        .all(|k| k.value.is_finite() && k.at >= 0.0 && k.at < 1.0)
                );
                c.set_key(KeyProperty::OffsetY, 0.5, 0.1, KeyEase::LINEAR);
                assert_eq!(
                    preset.keys(&c, 1.0 / 30.0, CanvasMotionControls::default()),
                    Err(PresetError::AlreadyKeyed)
                );
            }
        }
    }
    #[test]
    fn center_pop_is_not_a_duplicate_of_fade_and_scale() {
        let clip = Clip::blank("c1", "T1", ClipKind::Image, "image", 0.0, 4.0);
        let controls = CanvasMotionControls::default();
        let center = CanvasMotionPreset::CenterPop.keys(&clip, 1.0 / 30.0, controls).unwrap();
        let fade = CanvasMotionPreset::FadeScale.keys(&clip, 1.0 / 30.0, controls).unwrap();
        let first_opacity = |keys: &Vec<ClipKey>| keys.iter()
            .find(|k| k.property == KeyProperty::Opacity && k.at == 0.0)
            .unwrap().value;
        assert_eq!(first_opacity(&center), clip.opacity);
        assert_eq!(first_opacity(&fade), 0.0);
        assert_ne!(center, fade);
    }

    #[test]
    fn breathing_loop_returns_to_its_base_scale_at_the_last_visible_frame() {
        let clip = Clip::blank("c1", "T1", ClipKind::Video, "video", 0.0, 6.0);
        let keys = CanvasMotionPreset::BreathingLoop.keys(
            &clip, 1.0 / 30.0, CanvasMotionControls::default()
        ).unwrap();
        let scale: Vec<_> = keys.iter().filter(|k| k.property == KeyProperty::Scale).collect();
        assert!(scale.len() > 64);
        assert!((scale.first().unwrap().value - 0.92).abs() < 1e-8);
        assert!((scale.last().unwrap().value - 0.92).abs() < 1e-8);
        assert!(scale.iter().any(|k| k.value > 0.95));
        assert!(scale.iter().all(|k| k.value <= 1.0 && k.value >= 0.05));
        assert_eq!(CanvasMotionPreset::from_index(18), Some(CanvasMotionPreset::BreathingLoop));
        assert_eq!(CanvasMotionPreset::BreathingLoop.id(), "canvas-motion.breathing-loop");
    }

    #[test]
    fn in_hold_out_uses_independent_timing_and_auto_hold_by_default() {
        let clip = Clip::blank("c1", "T1", ClipKind::Image, "image", 0.0, 4.0);
        let frame = 1.0 / 30.0;
        let mut after = clip.clone();
        let keys = CanvasMotionPreset::InHoldOut.keys(
            &clip, frame, CanvasMotionControls::default()
        ).unwrap();
        for key in keys {
            after.set_key(key.property, key.at, key.value, key.ease);
        }
        assert_eq!(after.value_at(KeyProperty::Opacity, 0.0), 0.0);
        assert!(after.value_at(KeyProperty::Opacity, 0.05) > 0.0);
        assert!((after.value_at(KeyProperty::Opacity, 0.25) - clip.opacity).abs() < 1e-8);
        assert!((after.value_at(KeyProperty::Opacity, 0.8) - clip.opacity).abs() < 1e-8);
        assert!(after.value_at(KeyProperty::Opacity, 0.99) < clip.opacity);
        assert_eq!(CanvasMotionPreset::from_index(19), Some(CanvasMotionPreset::InHoldOut));

        let mut custom = clip.clone();
        for key in CanvasMotionPreset::InHoldOut.keys(
            &clip, frame, CanvasMotionControls {
                entry_seconds: 0.25, hold_seconds: 0.5, exit_seconds: 0.25,
                ..Default::default()
            }
        ).unwrap() {
            custom.set_key(key.property, key.at, key.value, key.ease);
        }
        // Explicit hold ends the animation early; remaining frames stay hidden.
        assert!(custom.value_at(KeyProperty::Opacity, 0.22) < clip.opacity);
        assert_eq!(custom.value_at(KeyProperty::Opacity, 0.5), 0.0);
    }

    #[test]
    fn focus_journey_reaches_and_holds_editable_middle_anchor() {
        let clip = Clip::blank("c1", "T1", ClipKind::Video, "video", 0.0, 4.0);
        let controls = CanvasMotionControls {
            focus_middle: [0.1, -0.1],
            focus_hold_seconds: 0.6,
            ..Default::default()
        };
        let keys = CanvasMotionPreset::FocusJourney.keys(&clip, 1.0 / 30.0, controls).unwrap();
        let mut after = clip.clone();
        for k in keys { after.set_key(k.property, k.at, k.value, k.ease); }
        // 3-second journey centers on its middle point at 1.5 seconds.
        assert!((after.value_at(KeyProperty::OffsetX, 1.5 / 4.0) - 0.1).abs() < 1e-8);
        assert!((after.value_at(KeyProperty::OffsetY, 1.5 / 4.0) + 0.1).abs() < 1e-8);
        assert!((after.value_at(KeyProperty::OffsetX, 1.4 / 4.0) - 0.1).abs() < 1e-8);
        assert!((after.value_at(KeyProperty::OffsetX, 1.6 / 4.0) - 0.1).abs() < 1e-8);
        assert_eq!(CanvasMotionPreset::from_index(20), Some(CanvasMotionPreset::FocusJourney));
        assert_eq!(CanvasMotionPreset::FocusJourney.id(), "canvas-motion.focus-journey");
    }

    #[test]
    fn reverse_delay_and_round_trip_are_one_undo() {
        let mut c = Clip::blank("c1", "T1", ClipKind::Image, "source", 0.0, 6.0);
        c.media_id = "m1".into();
        let mut p = Project::new();
        p.media.push(crate::model::MediaItem {
            id: "m1".into(),
            path: "source.jpg".into(),
            kind: crate::model::MediaKind::Image,
            ..Default::default()
        });
        p.active_mut().clips.push(Arc::new(c.clone()));
        let settings = DocumentSettings {
            name: "Motion".into(),
            width: 1920,
            height: 1080,
            rate_num: 30,
            rate_den: 1,
        };
        let mut editor = Editor::from_document(&crate::doc::to_document(&settings, &p)).unwrap();
        let controls = CanvasMotionControls {
            reverse: true,
            delay_seconds: 0.5,
            ..Default::default()
        };
        editor
            .apply(
                CanvasMotionPreset::TopRight
                    .command(&c, 1.0 / 30.0, controls)
                    .unwrap(),
            )
            .unwrap();
        let clip = editor.project().active().clip("c1").unwrap();
        assert!((clip.value_at(KeyProperty::Scale, 0.0) - 0.92).abs() < 1e-8);
        let saved = editor.to_document(&settings);
        let restored = Editor::from_document(&saved).unwrap();
        assert_eq!(
            clip.keys,
            restored.project().active().clip("c1").unwrap().keys
        );
        assert!(editor.undo());
        assert!(
            editor
                .project()
                .active()
                .clip("c1")
                .unwrap()
                .keys
                .is_empty()
        );
        assert!(editor.redo());
    }
    #[test]
    fn long_clip_short_motion_has_frame_dense_active_interval() {
        let clip = Clip::blank("c1", "T1", ClipKind::Video, "video", 0.0, 600.0);
        let frame = 1.0 / 30.0;
        let keys = CanvasMotionPreset::ALL[0].keys(&clip, frame,
            CanvasMotionControls {duration_seconds:1.0, ..Default::default()}).unwrap();
        let scale: Vec<_> = keys.iter().filter(|k|k.property == KeyProperty::Scale).collect();
        for f in 0..=30 {
            assert!(scale.iter().any(|k| (k.at * clip.duration / frame - f as f64).abs() < 1e-8));
        }
        assert!(scale.len() <= 68);
    }

    #[test]
    fn anchor_validation_accepts_displayed_endpoints_and_rejects_outside() {
        for value in [-0.45,0.45] {
            assert!(CanvasMotionControls {start_anchor:Some([value,value]),end_anchor:Some([value,value]),..Default::default()}.valid());
        }
        for value in [-0.4501,0.4501,f64::NAN] {
            assert!(!CanvasMotionControls {start_anchor:Some([value,0.0]),..Default::default()}.valid());
        }
    }

}
