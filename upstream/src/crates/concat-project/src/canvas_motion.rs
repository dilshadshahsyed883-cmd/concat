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
    OrbitIn,
    SpiralZoom,
    SCurveGlide,
    BoomerangReturn,
    PushPullZoom,
    FloatingLoop,
    EdgeSweep,
    ArcDrift,
    PinballJourney,
    CornerRelay,
    StaircaseMotion,
    WaveSurf,
    MagneticDock,
    RailSwitch,
}
impl CanvasMotionPreset {
    pub const ALL: [Self; 35] = [
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
        Self::OrbitIn,
        Self::SpiralZoom,
        Self::SCurveGlide,
        Self::BoomerangReturn,
        Self::PushPullZoom,
        Self::FloatingLoop,
        Self::EdgeSweep,
        Self::ArcDrift,
        Self::PinballJourney,
        Self::CornerRelay,
        Self::StaircaseMotion,
        Self::WaveSurf,
        Self::MagneticDock,
        Self::RailSwitch,
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
            Self::OrbitIn => "canvas-motion.orbit-in",
            Self::SpiralZoom => "canvas-motion.spiral-zoom",
            Self::SCurveGlide => "canvas-motion.s-curve-glide",
            Self::BoomerangReturn => "canvas-motion.boomerang-return",
            Self::PushPullZoom => "canvas-motion.push-pull-zoom",
            Self::FloatingLoop => "canvas-motion.floating-loop",
            Self::EdgeSweep => "canvas-motion.edge-sweep",
            Self::ArcDrift => "canvas-motion.arc-drift",
            Self::PinballJourney => "canvas-motion.pinball-journey",
            Self::CornerRelay => "canvas-motion.corner-relay",
            Self::StaircaseMotion => "canvas-motion.staircase-motion",
            Self::WaveSurf => "canvas-motion.wave-surf",
            Self::MagneticDock => "canvas-motion.magnetic-dock",
            Self::RailSwitch => "canvas-motion.rail-switch",
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
            // A boomerang must actually leave its launch point before
            // returning. The shared center scale peak is independent.
            Self::BoomerangReturn => ([-0.3, -0.12], [0.3, 0.18]),
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

    /// Sample each clip independently over its playable trimmed timeline
    /// duration. Auto Fit never uses the original media file's length.
    /// All presets use the same start/peak/end scale envelope while retaining
    /// their own spatial trajectories and opacity character.
    pub fn keys(
        self,
        clip: &Clip,
        frame: f64,
        controls: CanvasMotionControls,
    ) -> Result<Vec<ClipKey>, PresetError> {
        if !clip.kind.is_visual() {
            return Err(PresetError::NeedsPicture);
        }
        if !clip.duration.is_finite() || !frame.is_finite() || frame <= 0.0
            || clip.duration < frame * 2.0
        {
            return Err(PresetError::TooShort);
        }
        if [
            KeyProperty::Scale, KeyProperty::OffsetX,
            KeyProperty::OffsetY, KeyProperty::Opacity,
        ].iter().any(|property| clip.is_keyed(*property)) {
            return Err(PresetError::AlreadyKeyed);
        }
        if !controls.valid() {
            return Err(PresetError::NoChange);
        }
        let last = (clip.duration / frame - 1e-9).ceil() - 1.0;
        // A genuine start / middle / end envelope needs three visible
        // frames. Two frames cannot contain all three distinct checkpoints.
        if !last.is_finite() || last < 2.0 || last > i32::MAX as f64 {
            return Err(PresetError::TooShort);
        }
        // Auto Fit is exactly the clip's visible start to last frame. Manual
        // Duration may complete early; the remaining clip holds final values.
        let delay = if controls.auto_fit { 0.0 } else {
            (controls.delay_seconds / frame).round().clamp(0.0, last - 1.0)
        };
        let end = if controls.auto_fit { last } else {
            (delay + (controls.duration_seconds / frame).round().max(1.0)).min(last)
        };
        if end - delay < 2.0 {
            return Err(PresetError::TooShort);
        }
        let peak_frame = (delay + (end - delay) * controls.peak_time).round()
            .clamp(delay, end);
        let (mut from, mut to) = self.anchors(controls.seed);
        if let Some(value) = controls.start_anchor { from = value; }
        if let Some(value) = controls.end_anchor { to = value; }
        if matches!(self, Self::BreathingLoop | Self::FloatingLoop | Self::PushPullZoom) {
            from = [0.0, 0.0];
            to = [0.0, 0.0];
        }
        let samples = if matches!(self, Self::BreathingLoop | Self::FloatingLoop
            | Self::PinballJourney | Self::WaveSurf | Self::SpiralZoom) { 240 } else { 96 };
        let steps = ((end - delay) as usize).min(samples).max(1);
        let mut frames: Vec<f64> = (0..=steps)
            .map(|i| (delay + i as f64 * (end - delay) / steps as f64).round())
            .collect();
        frames.extend([0.0, delay, peak_frame, end, last]);
        if self == Self::InHoldOut {
            let active = end - delay;
            let entry = if controls.auto_fit {
                (active * 0.25).round().max(1.0)
            } else { (controls.entry_seconds / frame).round().max(1.0) };
            let exit = if controls.auto_fit {
                (active * 0.25).round().max(1.0)
            } else { (controls.exit_seconds / frame).round().max(1.0) };
            let exit_at = if controls.auto_fit || controls.hold_seconds == 0.0 {
                (end - exit).max(delay + entry)
            } else {
                (delay + entry + (controls.hold_seconds / frame).round())
                    .min(end - exit)
            };
            frames.extend([delay + entry, exit_at, (exit_at + exit).min(end)]);
        }
        frames.sort_by(f64::total_cmp);
        frames.dedup();
        let ease = |u: f64| -> f64 {
            let u = u.clamp(0.0, 1.0);
            match controls.easing {
                MotionEasing::Linear => u,
                MotionEasing::Smooth => u * u * (3.0 - 2.0 * u),
                MotionEasing::Cinematic => u * u * u * (u * (u * 6.0 - 15.0) + 10.0),
            }
        };
        let mut keys = Vec::with_capacity(frames.len() * 4);
        for f in frames {
            let u = ((f - delay) / (end - delay)).clamp(0.0, 1.0);
            let peak = ((peak_frame - delay) / (end - delay)).clamp(0.01, 0.99);
            let (start, finish, leg) = if u <= peak {
                (controls.start_scale, controls.middle_scale, ease(u / peak))
            } else {
                (controls.middle_scale, controls.end_scale,
                    ease((u - peak) / (1.0 - peak)))
            };
            let mut scale = start + (finish - start) * leg;
            if self == Self::CenterPop {
                // A fast impulse followed by a softer settle, unlike the
                // ordinary centered Zoom. The pulse is zero at checkpoints.
                let q = if u <= peak { u / peak }
                    else { (u - peak) / (1.0 - peak) };
                scale += if u <= peak {
                    0.10 * (std::f64::consts::PI * q).sin() * (1.0 - q)
                } else {
                    0.035 * (2.0 * std::f64::consts::PI * q).sin() * (1.0 - q)
                };
            } else if self == Self::SpringSettle {
                // The spring has a damped overshoot/return, not a second copy
                // of centered Zoom; both endpoints and the peak are exact.
                let q = if u <= peak { u / peak }
                    else { (u - peak) / (1.0 - peak) };
                scale += 0.075 * (3.0 * std::f64::consts::PI * q).sin()
                    * (-2.0 * q).exp() * (1.0 - q);
            } else if self == Self::PushPullZoom {
                // Alternating inward/outward impulse within each leg. The
                // universal Start/Peak/End values remain intact.
                let q = if u <= peak { u / peak }
                    else { (u - peak) / (1.0 - peak) };
                scale += 0.075 * (2.0 * std::f64::consts::PI * q).sin()
                    * (std::f64::consts::PI * q).sin();
            }
            if self == Self::BreathingLoop {
                // Exactly zero supplemental pulse at start, peak and end.
                // The integer wave count makes restarts repeat-safe.
                let cycles = ((clip.duration * controls.speed
                    / controls.duration_seconds).round() as i32).clamp(1, 12) as f64;
                let wave = (std::f64::consts::TAU * cycles * u).sin().powi(2);
                let envelope = u * (1.0 - u) * (u - peak).abs();
                scale += 0.12 * controls.strength * wave * envelope;
            }
            scale = scale.clamp(0.05, 1.0);
            let reverse = controls.reverse ^ (self == Self::ReverseCorner);
            let path_time = if reverse { 1.0 - u } else { u };
            // Speed changes trajectory cadence but never shortens Auto Fit or
            // moves the scale peak. Both path endpoints remain deterministic.
            let t = ease(path_time.powf(1.0 / controls.speed));
            let interpolate = |a: [f64;2], b: [f64;2], q: f64| -> [f64;2] {
                [a[0] + (b[0] - a[0]) * q, a[1] + (b[1] - a[1]) * q]
            };
            let path = match self {
                Self::OrbitIn => {
                    let radius = 0.3 * (1.0 - t);
                    [radius * (1.5 * std::f64::consts::PI * t).cos(),
                        radius * (1.5 * std::f64::consts::PI * t).sin()]
                }
                Self::SpiralZoom => {
                    let angle = 3.0 * std::f64::consts::PI * t;
                    [0.32 * (1.0 - t) * angle.cos(),
                        0.32 * (1.0 - t) * angle.sin()]
                }
                Self::SCurveGlide => [-0.3 + 0.6 * t,
                    0.2 * (2.0 * t - 1.0) * (std::f64::consts::PI * t).sin()],
                Self::BoomerangReturn => {
                    let reach = 1.0 - (2.0 * t - 1.0).abs();
                    interpolate(from, to, ease(reach))
                }
                Self::PushPullZoom => [0.0, 0.0],
                Self::FloatingLoop => {
                    let cycles = (controls.speed * 2.0).round().clamp(1.0, 5.0);
                    [0.12 * (std::f64::consts::TAU * cycles * t).sin(),
                        0.06 * ((std::f64::consts::TAU * cycles * t).cos() - 1.0)]
                }
                Self::EdgeSweep => [-0.38 + 0.76 * t, 0.0],
                Self::ArcDrift => [-0.3 + 0.6 * t,
                    -0.22 * (std::f64::consts::PI * t).sin()],
                Self::PinballJourney => {
                    let spots = [[-0.3,-0.25],[0.3,0.25],[-0.3,0.25],[0.0,0.0]];
                    let leg = (t * 3.0).min(2.999999);
                    let n = leg.floor() as usize;
                    interpolate(spots[n], spots[n+1], ease(leg - n as f64))
                }
                Self::CornerRelay => {
                    let spots = [[-0.3,-0.3],[0.3,-0.3],[0.3,0.3],[-0.3,0.3],[0.0,0.0]];
                    let leg = (t * 4.0).min(3.999999);
                    let n = leg.floor() as usize;
                    interpolate(spots[n], spots[n+1], ease(leg - n as f64))
                }
                Self::StaircaseMotion => {
                    let q = (t * 6.0).min(5.999999);
                    let step = q.floor() as i32;
                    let next = ease(((q - step as f64) / 0.75).clamp(0.0,1.0));
                    let x = -0.3 + ((step + 1) / 2) as f64 * 0.2
                        + if step % 2 == 0 { 0.2 * next } else { 0.0 };
                    let y = -0.3 + (step / 2) as f64 * 0.2
                        + if step % 2 != 0 { 0.2 * next } else { 0.0 };
                    [x.min(0.3),y.min(0.3)]
                }
                Self::WaveSurf => [-0.35 + 0.7 * t,
                    0.15 * (4.0 * std::f64::consts::PI * t).sin()],
                Self::MagneticDock => {
                    let x = if t < 0.25 { 0.32 * ease(t / 0.25) }
                        else if t < 0.6 { 0.32 }
                        else { 0.32 * (1.0 - ease((t-0.6)/0.4)) };
                    [x, 0.0]
                }
                Self::RailSwitch => [-0.34 + 0.68 * t,
                    -0.22 + 0.44 * ease(((t - 0.42) / 0.16).clamp(0.0,1.0))],
                Self::FocusJourney => {
                    let hold = if controls.auto_fit {
                        (controls.focus_hold_seconds / clip.duration).clamp(0.0, 0.8)
                    } else {
                        (controls.focus_hold_seconds / controls.duration_seconds).clamp(0.0, 0.8)
                    };
                    let span = (1.0 - hold) / 2.0;
                    if t < span {
                        interpolate(from, controls.focus_middle, ease(t / span))
                    } else if t < span + hold {
                        controls.focus_middle
                    } else {
                        interpolate(controls.focus_middle, to, ease((t - span - hold)/span))
                    }
                }
                Self::Alternating => {
                    let base = interpolate(from, to, t);
                    [base[0] + 0.08 * (2.0 * std::f64::consts::PI * t).sin() * (1.0-t),
                        base[1] + 0.04 * (4.0 * std::f64::consts::PI * t).sin() * (1.0-t)]
                }
                Self::SpringSettle => {
                    let damp = (0.5 / controls.intensity).clamp(0.2, 0.95);
                    let spring = if t >= 1.0 { 1.0 } else {
                        1.0 - (-12.0 * damp * t).exp()
                            * (12.0 * (1.0 - damp * damp).sqrt() * t).cos()
                    };
                    interpolate(from, to, spring)
                }
                _ => interpolate(from, to, t),
            };
            let room = (1.0 - scale) / 2.0;
            let x = (path[0] * controls.strength).clamp(-room, room);
            let y = (path[1] * controls.strength).clamp(-room, room);
            let opacity = if self == Self::FadeScale {
                clip.opacity * ease(u)
            } else if self == Self::InHoldOut {
                let active = end - delay;
                let entry = if controls.auto_fit {
                    (active * 0.25).round().max(1.0)
                } else { (controls.entry_seconds / frame).round().max(1.0) };
                let exit = if controls.auto_fit {
                    (active * 0.25).round().max(1.0)
                } else { (controls.exit_seconds / frame).round().max(1.0) };
                let exit_at = if controls.auto_fit || controls.hold_seconds == 0.0 {
                    (end - exit).max(delay + entry)
                } else {
                    (delay + entry + (controls.hold_seconds / frame).round()).min(end - exit)
                };
                clip.opacity * ease((f - delay) / entry)
                    * (1.0 - ease((f - exit_at) / exit))
            } else { clip.opacity };
            for (property, value) in [
                (KeyProperty::Scale, scale),
                (KeyProperty::OffsetX, x),
                (KeyProperty::OffsetY, y),
                (KeyProperty::Opacity, opacity),
            ] {
                keys.push(ClipKey {
                    property, at: f * frame / clip.duration,
                    value, ease: KeyEase::LINEAR,
                });
            }
        }
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
    Cinematic,
}
#[derive(Clone, Copy, Debug, serde::Serialize, serde::Deserialize)]
pub struct CanvasMotionControls {
    pub start_scale: f64,
    /// Shared three-checkpoint scale curve, independent of spatial trajectory.
    #[serde(default = "default_middle_scale")]
    pub middle_scale: f64,
    /// Relative progress for the peak, between 20% and 80%.
    #[serde(default = "default_peak_time")]
    pub peak_time: f64,
    /// Default: scale the active duration to each visible timeline clip.
    #[serde(default = "default_auto_fit")]
    pub auto_fit: bool,
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
fn default_middle_scale() -> f64 { 1.0 }
fn default_peak_time() -> f64 { 0.5 }
fn default_auto_fit() -> bool { true }

impl Default for CanvasMotionControls {
    fn default() -> Self {
        Self {
            start_scale: 0.70,
            middle_scale: 1.0,
            peak_time: 0.5,
            auto_fit: true,
            end_scale: 0.70,
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
        // Older saved values below the new inspector's 40% floor remain
        // loadable rather than being silently discarded on upgrade.
        [self.start_scale, self.end_scale]
            .iter()
            .all(|v| v.is_finite() && (0.05..=1.0).contains(v))
            && self.middle_scale.is_finite()
            && (0.5..=1.0).contains(&self.middle_scale)
            && self.peak_time.is_finite()
            && (0.2..=0.8).contains(&self.peak_time)
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
            && (0.0..=2.0).contains(&self.strength)
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
    fn fewer_than_three_visible_frames_never_fake_a_middle_peak() {
        let frame=1.0/30.0;
        for duration in [frame,2.0*frame] {
            let clip=Clip::blank("short","T1",ClipKind::Image,"image",0.0,duration);
            assert_eq!(CanvasMotionPreset::TopLeft.keys(
                &clip,frame,CanvasMotionControls::default()),Err(PresetError::TooShort));
        }
        let clip=Clip::blank("three","T1",ClipKind::Video,"video",0.0,3.0*frame);
        let keys=CanvasMotionPreset::TopLeft.keys(
            &clip,frame,CanvasMotionControls::default()).unwrap();
        let scale: Vec<_>=keys.iter().filter(|key|key.property==KeyProperty::Scale).collect();
        assert_eq!(scale.first().unwrap().value,0.70);
        assert_eq!(scale.last().unwrap().value,0.70);
        assert!(scale.iter().any(|key|key.value==1.0));
    }

    #[test]
    fn all_canvas_presets_fit_visible_clip_and_share_start_peak_end_scale() {
        let frame = 1.0 / 30.0;
        for seconds in [2.0,4.0,6.0,10.0] {
            let clip=Clip::blank("c1","T1",ClipKind::Video,"video",0.0,seconds);
            let last=(seconds/frame-1e-9).ceil()-1.0;
            for preset in CanvasMotionPreset::ALL {
                let keys=preset.keys(&clip,frame,CanvasMotionControls::default()).unwrap();
                let scales: Vec<_>=keys.iter().filter(|k|k.property==KeyProperty::Scale).collect();
                assert!((scales.first().unwrap().value-0.70).abs()<1e-8,"{:?}",preset);
                assert!((scales.last().unwrap().value-0.70).abs()<1e-8,"{:?}",preset);
                assert!(scales.iter().any(|k|(k.at*seconds/frame-(last*0.5).round()).abs()<1e-6
                    && (k.value-1.0).abs()<1e-8),"{:?}",preset);
                assert!(scales.iter().all(|k|k.at>=0.0 && k.at<1.0));
            }
        }
    }

    #[test]
    fn every_canvas_entry_has_a_distinct_native_motion_signature() {
        let clip=Clip::blank("signature","T1",ClipKind::Video,"video",0.0,6.0);
        let variants=CanvasMotionPreset::ALL;
        let signatures: Vec<_>=variants.iter().map(|preset| {
            (*preset,preset.keys(&clip,1.0/30.0,CanvasMotionControls::default()).unwrap())
        }).collect();
        for (index,(first,a)) in signatures.iter().enumerate() {
            for (second,b) in signatures.iter().skip(index+1) {
                assert_ne!(a,b,"Canvas {:?} duplicates {:?}",first,second);
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
        assert!((scale.first().unwrap().value - 0.70).abs() < 1e-8);
        assert!((scale.last().unwrap().value - 0.70).abs() < 1e-8);
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
        assert!(after.value_at(KeyProperty::Opacity, 0.8) < clip.opacity);
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
        // Auto Fit intentionally overrides manual Entry/Hold/Exit seconds.
        assert_eq!(custom.value_at(KeyProperty::Opacity, 0.0), 0.0);
        assert!(custom.value_at(KeyProperty::Opacity, 0.5) > 0.0);
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
        // Full-size midpoint must be centered to avoid off-canvas clipping.
        assert!(after.value_at(KeyProperty::OffsetX, 0.5).abs() < 1e-8);
        assert!(after.value_at(KeyProperty::OffsetY, 0.5).abs() < 1e-8);
        assert!(after.value_at(KeyProperty::OffsetX, 0.25).abs() > 0.0);
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
        assert!((clip.value_at(KeyProperty::Scale, 0.0) - 0.70).abs() < 1e-8);
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
            CanvasMotionControls {auto_fit:false,duration_seconds:1.0, ..Default::default()}).unwrap();
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
