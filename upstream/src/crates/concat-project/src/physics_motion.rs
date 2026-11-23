// SPDX-License-Identifier: AGPL-3.0-or-later

//! Physics-driven motion sampled into Concat's existing, GPU-compatible clip keys.
//! These are deterministic, closed-form simulations: no random frame jitter, no
//! new project fields, no CPU video-filter fallback. Desktop and CUDA both
//! interpolate the same native keyframes after save/reload.

use std::f64::consts::PI;

use crate::Command;
use crate::model::{Clip, ClipKey, ClipKind, KeyEase, KeyProperty, ranges};
use crate::motion_preset::PresetError;

/// Stable IDs are used by app preferences, never by the project manifest.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum PhysicsPreset {
    SpringZoom,
    Pendulum,
    GravityBounce,
    ElasticPop,
    InertiaGlide,
    DampedShake,
    SpringSlide,
    FloatingBob,
    RecoilKick,
    SpiralZoom,
    OrbitMotion,
    ZigzagDrift,
    TumbleSettle,
}

/// Parameters are baked into ordinary GPU-compatible keyframes. The project
/// manifest does not need a desktop-only physics extension.
#[derive(Clone, Copy, Debug, serde::Serialize, serde::Deserialize)]
pub struct PhysicsControls {
    pub strength: f64,
    /// Fraction of the clip over which the motion evolves (0.1..=1).
    pub duration: f64,
    pub speed: f64,
    /// Energy/springiness: higher values retain oscillation for longer.
    pub intensity: f64,
}

impl Default for PhysicsControls {
    fn default() -> Self {
        Self { strength: 1.0, duration: 1.0, speed: 1.0, intensity: 1.0 }
    }
}

impl PhysicsControls {
    /// Whether every control is in its supported authoring range.
    pub fn valid(self)->bool {
        [self.strength,self.duration,self.speed,self.intensity].iter().all(|v|v.is_finite())
            && (0.1..=2.0).contains(&self.strength) && (0.1..=1.0).contains(&self.duration)
            && (0.25..=3.0).contains(&self.speed) && (0.2..=2.0).contains(&self.intensity)
    }
    fn safe(self) -> Self {
        fn limit(value: f64, min: f64, max: f64) -> f64 {
            if value.is_finite() { value.clamp(min, max) } else { 1.0 }
        }
        Self {
            strength: limit(self.strength, 0.1, 2.0),
            duration: limit(self.duration, 0.1, 1.0),
            speed: limit(self.speed, 0.25, 3.0),
            intensity: limit(self.intensity, 0.2, 2.0),
        }
    }
}

impl PhysicsPreset {
    pub const ALL: [Self; 13] = [
        Self::SpringZoom,
        Self::Pendulum,
        Self::GravityBounce,
        Self::ElasticPop,
        Self::InertiaGlide,
        Self::DampedShake,
        Self::SpringSlide,
        Self::FloatingBob,
        Self::RecoilKick,
        Self::SpiralZoom,
        Self::OrbitMotion,
        Self::ZigzagDrift,
        Self::TumbleSettle,
    ];

    pub fn from_index(index: i32) -> Option<Self> {
        usize::try_from(index).ok().and_then(|i| Self::ALL.get(i).copied())
    }

    pub const fn id(self) -> &'static str {
        match self {
            Self::SpringZoom => "physics.spring-zoom",
            Self::Pendulum => "physics.pendulum",
            Self::GravityBounce => "physics.gravity-bounce",
            Self::ElasticPop => "physics.elastic-pop",
            Self::InertiaGlide => "physics.inertia-glide",
            Self::DampedShake => "physics.damped-shake",
            Self::SpringSlide => "physics.spring-slide",
            Self::FloatingBob => "physics.floating-bob",
            Self::RecoilKick => "physics.recoil-kick",
            Self::SpiralZoom => "physics.spiral-zoom",
            Self::OrbitMotion => "physics.orbit-motion",
            Self::ZigzagDrift => "physics.zigzag-drift",
            Self::TumbleSettle => "physics.tumble-settle",
        }
    }

    fn property(self) -> KeyProperty {
        match self {
            Self::SpringZoom | Self::ElasticPop => KeyProperty::Scale,
            Self::Pendulum | Self::DampedShake => KeyProperty::Rotation,
            Self::GravityBounce => KeyProperty::OffsetY,
            Self::InertiaGlide => KeyProperty::OffsetX,
            Self::SpringSlide | Self::RecoilKick | Self::OrbitMotion | Self::ZigzagDrift =>
                KeyProperty::OffsetX,
            Self::FloatingBob => KeyProperty::OffsetY,
            Self::SpiralZoom | Self::TumbleSettle => KeyProperty::Scale,
        }
    }

    fn extended_properties(self) -> &'static [KeyProperty] {
        match self {
            Self::SpringSlide => &[KeyProperty::OffsetX],
            Self::FloatingBob => &[KeyProperty::OffsetY],
            Self::RecoilKick => &[KeyProperty::OffsetX, KeyProperty::Rotation],
            Self::SpiralZoom => &[KeyProperty::Scale, KeyProperty::Rotation],
            Self::OrbitMotion | Self::ZigzagDrift => &[KeyProperty::OffsetX, KeyProperty::OffsetY],
            Self::TumbleSettle => &[KeyProperty::Rotation, KeyProperty::Scale],
            _ => &[],
        }
    }

    /// Seven new motions share the existing four controls and GPU-native channels.
    /// All paths are deterministic and settle or loop at a stable clip boundary.
    fn extended_value(
        self, clip: &Clip, property: KeyProperty, t: f64, controls: PhysicsControls,
    ) -> f64 {
        let c = controls.safe();
        let u = ((t.clamp(0.0, 1.0) / c.duration).min(1.0) * c.speed).min(1.0);
        let fade = (-5.0 * u / c.intensity).exp();
        let result = match (self, property) {
            (Self::SpringSlide, KeyProperty::OffsetX) =>
                clip.offset_x - 0.18 * fade * (6.0 * PI * u).cos(),
            (Self::FloatingBob, KeyProperty::OffsetY) =>
                clip.offset_y + 0.045 * (4.0 * PI * u).sin(),
            (Self::RecoilKick, KeyProperty::OffsetX) =>
                clip.offset_x - 0.085 * fade * (3.0 * PI * u).sin(),
            (Self::RecoilKick, KeyProperty::Rotation) =>
                clip.rotation - 9.0 * fade * (3.0 * PI * u).sin(),
            (Self::SpiralZoom, KeyProperty::Scale) =>
                clip.scale * (1.0 + 0.16 * (1.0 - (-6.0 * u).exp())),
            (Self::SpiralZoom, KeyProperty::Rotation) =>
                clip.rotation + 15.0 * fade * (5.0 * PI * u).sin(),
            (Self::OrbitMotion, KeyProperty::OffsetX) =>
                clip.offset_x + 0.08 * ((2.0 * PI * u).cos() - 1.0),
            (Self::OrbitMotion, KeyProperty::OffsetY) =>
                clip.offset_y + 0.06 * (2.0 * PI * u).sin(),
            (Self::ZigzagDrift, KeyProperty::OffsetX) =>
                clip.offset_x + 0.10 * fade * (4.0 * PI * u).sin(),
            (Self::ZigzagDrift, KeyProperty::OffsetY) =>
                clip.offset_y + 0.04 * fade * (8.0 * PI * u).sin(),
            (Self::TumbleSettle, KeyProperty::Rotation) =>
                clip.rotation + 20.0 * fade * (5.0 * PI * u).cos(),
            (Self::TumbleSettle, KeyProperty::Scale) =>
                clip.scale * (1.0 + 0.12 * fade * (5.0 * PI * u).cos()),
            _ => unreachable!("only supported extended properties are sampled"),
        };
        let base = match property {
            KeyProperty::Scale => clip.scale,
            KeyProperty::Rotation => clip.rotation,
            KeyProperty::OffsetX => clip.offset_x,
            KeyProperty::OffsetY => clip.offset_y,
            _ => unreachable!(),
        };
        base + (result - base) * c.strength
    }

    fn extended_keys_with(
        self, clip: &Clip, frame: f64, controls: PhysicsControls,
    ) -> Result<(Vec<ClipKey>, f64), PresetError> {
        if !matches!(clip.kind, ClipKind::Image | ClipKind::Video) {
            return Err(PresetError::NeedsPicture);
        }
        if !frame.is_finite() || frame <= 0.0 || !clip.duration.is_finite() {
            return Err(PresetError::TooShort);
        }
        let last = (clip.duration / frame - 1e-9).ceil() - 1.0;
        // Oscillation needs at least three visible positions; two would alias a full loop.
        if !last.is_finite() || last < 2.0 || last > i32::MAX as f64 {
            return Err(PresetError::TooShort);
        }
        let properties = self.extended_properties();
        if properties.iter().any(|property| clip.is_keyed(*property)) {
            return Err(PresetError::AlreadyKeyed);
        }
        let controls = controls.safe();
        let active = (last * controls.duration).ceil().clamp(2.0, last);
        let steps = (active as usize)
            .min((96.0 * controls.speed.max(1.0)).ceil() as usize).min(240).max(2);
        let mut frames: Vec<f64> = (0..=steps)
            .map(|i| (i as f64 * active / steps as f64).round()).collect();
        frames.push(last);
        frames.sort_by(f64::total_cmp);
        frames.dedup();
        let mut keys = Vec::with_capacity(frames.len() * properties.len());
        let mut any_movement = false;
        for &property in properties {
            let base = match property {
                KeyProperty::Scale => clip.scale,
                KeyProperty::Rotation => clip.rotation,
                KeyProperty::OffsetX => clip.offset_x,
                KeyProperty::OffsetY => clip.offset_y,
                _ => unreachable!(),
            };
            for &at in &frames {
                let value = self.extended_value(clip, property, at / last, controls);
                let value = match property {
                    KeyProperty::Scale => value.clamp(ranges::MIN_SCALE, ranges::MAX_SCALE),
                    KeyProperty::OffsetX | KeyProperty::OffsetY =>
                        value.clamp(-ranges::MAX_OFFSET, ranges::MAX_OFFSET),
                    KeyProperty::Rotation => value.clamp(-360.0, 360.0),
                    _ => unreachable!(),
                };
                if !value.is_finite() { return Err(PresetError::NoChange); }
                any_movement |= (value - base).abs() > 1e-9;
                keys.push(ClipKey {
                    property, at: at * frame / clip.duration,
                    value, ease: KeyEase::LINEAR,
                });
            }
        }
        if !any_movement { return Err(PresetError::NoChange); }
        let preview = clip.start + (last / 2.0).round() * frame;
        Ok((keys, preview))
    }

    /// Sample physically modelled displacement at normalized clip time.
    /// Motion is expressed in the same transform units as the native editor.
    fn value(self, clip: &Clip, t: f64) -> f64 {
        self.value_with(clip, t, PhysicsControls::default())
    }

    fn value_with(self, clip: &Clip, t: f64, controls: PhysicsControls) -> f64 {
        if !self.extended_properties().is_empty() {
            return self.extended_value(clip, self.property(), t, controls);
        }
        let controls = controls.safe();
        let t = (t.clamp(0.0, 1.0) / controls.duration).min(1.0) * controls.speed;
        let base = match self.property() {
            KeyProperty::Scale => clip.scale,
            KeyProperty::Rotation => clip.rotation,
            KeyProperty::OffsetX => clip.offset_x,
            KeyProperty::OffsetY => clip.offset_y,
            _ => unreachable!(),
        };
        let original = match self {
            Self::SpringZoom => {
                // Mass-spring-damper step response: damping ratio 0.45.
                let zeta: f64 = (0.45 / controls.intensity).clamp(0.05, 0.95);
                let natural_frequency: f64 = 18.0;
                let damped = natural_frequency * (1.0 - zeta * zeta).sqrt();
                let response = 1.0 - (-zeta * natural_frequency * t).exp()
                    * ((damped * t).cos()
                        + zeta / (1.0 - zeta * zeta).sqrt() * (damped * t).sin());
                clip.scale * (1.0 + 0.12 * response)
            }
            Self::Pendulum => {
                // Small-angle damped pendulum, with angular drag.
                clip.rotation + 9.0 * (-4.5 * t / controls.intensity).exp() * (6.0 * PI * t).cos()
            }
            Self::GravityBounce => {
                // Ballistic fall and restitution at each ground impact.
                // Gravity is chosen so the initial 0.18-frame fall lasts .32.
                let height = 0.18;
                let fall = 0.32;
                let gravity = 2.0 * height / (fall * fall);
                if t < fall {
                    let left = 1.0 - t / fall;
                    clip.offset_y - height * left * left
                } else {
                    let mut remaining = t - fall;
                    let restitution = (0.56 * controls.intensity).clamp(0.1, 0.95);
                    let mut velocity = (2.0 * gravity * height).sqrt() * restitution;
                    let mut lift = 0.0;
                    for _ in 0..12 {
                        let flight = 2.0 * velocity / gravity;
                        if remaining < flight {
                            lift = (velocity * remaining - 0.5 * gravity * remaining * remaining).max(0.0);
                            break;
                        }
                        remaining -= flight;
                        velocity *= restitution;
                    }
                    clip.offset_y - lift
                }
            }
            Self::ElasticPop => {
                // Underdamped elastic impulse, preserving the initial framing.
                clip.scale * (1.0 + 0.17 * (-5.0 * t / controls.intensity).exp() * (19.0 * t).sin())
            }
            Self::InertiaGlide => {
                // Exponential velocity decay with near-zero residual motion.
                clip.offset_x - 0.14 * ((-5.0 * t / controls.intensity).exp() - (-5.0_f64 / controls.intensity).exp())
            }
            Self::DampedShake => {
                // Two modes of a decaying oscillator; deterministic per frame.
                clip.rotation + (-5.5 * t / controls.intensity).exp()
                    * (5.0 * (23.0 * t).sin() + 1.5 * (39.0 * t).sin())
            }
            _ => unreachable!("extended motion handled above"),
        };
        base + (original - base) * controls.strength
    }

    pub fn keys(self, clip: &Clip, frame: f64) -> Result<(Vec<ClipKey>, f64), PresetError> {
        self.keys_with(clip, frame, PhysicsControls::default())
    }

    pub fn keys_with(
        self, clip: &Clip, frame: f64, controls: PhysicsControls,
    ) -> Result<(Vec<ClipKey>, f64), PresetError> {
        if !self.extended_properties().is_empty() {
            return self.extended_keys_with(clip, frame, controls);
        }
        if !matches!(clip.kind, ClipKind::Image | ClipKind::Video) {
            return Err(PresetError::NeedsPicture);
        }
        if !frame.is_finite() || frame <= 0.0 || !clip.duration.is_finite() {
            return Err(PresetError::TooShort);
        }
        let last = (clip.duration / frame - 1e-9).ceil() - 1.0;
        if !last.is_finite() || last < 1.0 || last > i32::MAX as f64 {
            return Err(PresetError::TooShort);
        }
        let property = self.property();
        if clip.is_keyed(property) {
            return Err(PresetError::AlreadyKeyed);
        }
        // Spend the key budget on the active motion, not the inactive tail.
        // Faster oscillators get more points; short motion gets every frame.
        let controls = controls.safe();
        let active = (last * controls.duration).ceil().clamp(1.0, last);
        let steps = (active as usize).min((64.0 * controls.speed.max(1.0)).ceil() as usize).min(240);
        let mut frames: Vec<f64> = (0..=steps)
            .map(|sample| (sample as f64 * active / steps as f64).round()).collect();
        frames.push(last);
        frames.sort_by(f64::total_cmp);
        frames.dedup();
        let mut keys = Vec::with_capacity(frames.len());
        for at_frame in frames {
            let t = at_frame / last;
            let value = self.value_with(clip, t, controls);
            let value = match property {
                KeyProperty::Scale => value.clamp(ranges::MIN_SCALE, ranges::MAX_SCALE),
                KeyProperty::OffsetX | KeyProperty::OffsetY =>
                    value.clamp(-ranges::MAX_OFFSET, ranges::MAX_OFFSET),
                _ => value,
            };
            if !value.is_finite() {
                return Err(PresetError::NoChange);
            }
            keys.push(ClipKey {
                property,
                at: at_frame * frame / clip.duration,
                value,
                ease: KeyEase::LINEAR,
            });
        }
        if keys.iter().all(|key| (key.value - keys[0].value).abs() < 1e-10) {
            return Err(PresetError::NoChange);
        }
        let preview = clip.start + (last / 2.0).round() * frame;
        Ok((keys, preview))
    }

    pub fn command(self, clip: &Clip, frame: f64) -> Result<(Command, f64), PresetError> {
        self.command_with(clip, frame, PhysicsControls::default())
    }

    pub fn command_with(
        self, clip: &Clip, frame: f64, controls: PhysicsControls,
    ) -> Result<(Command, f64), PresetError> {
        let (keys, preview) = self.keys_with(clip, frame, controls)?;
        let commands = keys.into_iter().map(|key| Command::SetClipKey {
            clip_id: clip.id.clone(),
            property: key.property,
            at: key.at,
            value: key.value,
            ease: key.ease,
        }).collect();
        Ok((Command::Batch { commands }, preview))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FRAME: f64 = 1.0 / 30.0;

    #[test]
    fn original_six_presets_generate_finite_frame_aligned_native_keys() {
        for preset in PhysicsPreset::ALL.into_iter().take(6) {
            for kind in [ClipKind::Image, ClipKind::Video] {
                let clip = Clip::blank("c1", "t1", kind, "picture", 0.0, 3.0);
                let (keys, preview) = preset.keys(&clip, FRAME).unwrap();
                assert!(keys.len() >= 3 && keys.len() <= 65);
                assert!(preview > clip.start && preview < clip.start + clip.duration);
                assert!(keys.windows(2).all(|pair| pair[0].at < pair[1].at));
                assert!(keys.iter().all(|key| key.value.is_finite() &&
                    key.ease == KeyEase::LINEAR && key.at >= 0.0 && key.at < 1.0));
                let (Command::Batch { commands }, _) = preset.command(&clip, FRAME).unwrap()
                    else { panic!("motion must be one undoable batch") };
                assert_eq!(commands.len(), keys.len());
            }
        }
    }

    #[test]
    fn one_and_two_frame_clips_are_safe() {
        let single = Clip::blank("c1", "t1", ClipKind::Image, "picture", 0.0, FRAME);
        let pair = Clip::blank("c2", "t1", ClipKind::Video, "picture", 0.0, 2.0 * FRAME);
        for preset in PhysicsPreset::ALL.into_iter().take(6) {
            assert_eq!(preset.keys(&single, FRAME).unwrap_err(), PresetError::TooShort);
            let (keys, _) = preset.keys(&pair, FRAME).unwrap();
            assert_eq!(keys.len(), 2);
        }
    }

    #[test]
    fn adjustable_controls_are_baked_and_remain_native_keys() {
        let clip = Clip::blank("c1", "t1", ClipKind::Image, "picture", 0.0, 4.0);
        let normal = PhysicsPreset::DampedShake.keys(&clip, FRAME).unwrap().0;
        let stronger = PhysicsPreset::DampedShake.keys_with(
            &clip, FRAME, PhysicsControls {
                strength: 1.8, duration: 0.5, speed: 1.5, intensity: 1.7,
            },
        ).unwrap().0;
        assert!(stronger.len() <= 242);
        assert_ne!(normal, stronger);
        assert!(stronger.iter().all(|key| key.value.is_finite()));
        let defaults = PhysicsPreset::DampedShake.keys_with(
            &clip, FRAME, PhysicsControls::default(),
        ).unwrap().0;
        assert_eq!(normal, defaults);
        for preset in PhysicsPreset::ALL {
            for intensity in [0.2, 0.5, 1.0, 2.0] {
                let varied = preset.keys_with(&clip, FRAME, PhysicsControls {
                    strength: 2.0, duration: 0.3, speed: 3.0, intensity,
                }).unwrap().0;
                assert!(varied.iter().all(|key| key.value.is_finite()));
            }
        }
    }

    #[test]
    fn strength_and_speed_change_baked_motion_without_project_extensions() {
        let clip = Clip::blank("c1", "t1", ClipKind::Image, "photo", 0.0, 3.0);
        let normal = PhysicsPreset::SpringZoom.keys(&clip, FRAME).unwrap().0;
        let strong = PhysicsPreset::SpringZoom.keys_with(
            &clip, FRAME, PhysicsControls {
                strength: 2.0, ..PhysicsControls::default()
            },
        ).unwrap().0;
        let quick = PhysicsPreset::SpringZoom.keys_with(
            &clip, FRAME, PhysicsControls {
                duration: 0.5, speed: 2.0, ..PhysicsControls::default()
            },
        ).unwrap().0;
        assert_eq!(normal.len(), strong.len());
        assert!(quick.len() <= 242);
        assert!(quick.windows(2).all(|pair| pair[0].at < pair[1].at));
        assert_ne!(normal, strong);
        assert_ne!(normal, quick);
    }

    #[test]
    fn manual_keys_are_never_overwritten() {
        let mut clip = Clip::blank("c1", "t1", ClipKind::Image, "picture", 0.0, 4.0);
        clip.set_key(KeyProperty::Scale, 0.25, 1.5, KeyEase::LINEAR);
        assert_eq!(PhysicsPreset::SpringZoom.keys(&clip, FRAME).unwrap_err(),
                   PresetError::AlreadyKeyed);
        assert!(PhysicsPreset::Pendulum.keys(&clip, FRAME).is_ok());
    }

    #[test]
    fn physics_is_repeatable_and_bounded() {
        let clip = Clip::blank("c1", "t1", ClipKind::Image, "picture", 0.0, 2.0);
        for preset in PhysicsPreset::ALL {
            let a = preset.keys(&clip, FRAME).unwrap().0;
            let b = preset.keys(&clip, FRAME).unwrap().0;
            assert_eq!(a, b);
            for key in a {
                if key.property == KeyProperty::Scale {
                    assert!(key.value >= ranges::MIN_SCALE && key.value <= ranges::MAX_SCALE);
                }
                if matches!(key.property, KeyProperty::OffsetX | KeyProperty::OffsetY) {
                    assert!(key.value.abs() <= ranges::MAX_OFFSET);
                }
            }
        }
    }
    #[test]
    fn expanded_physics_presets_are_deterministic_multi_channel_and_protect_manual_keys() {
        let clip = Clip::blank("c1", "t1", ClipKind::Video, "picture", 0.0, 3.0);
        for preset in PhysicsPreset::ALL.into_iter().skip(6) {
            let (keys, preview) = preset.keys(&clip, FRAME).unwrap();
            assert!(preview > clip.start && preview < clip.start + clip.duration);
            assert!(keys.iter().all(|key| key.at >= 0.0 && key.at < 1.0 && key.value.is_finite()));
            assert_eq!(keys, preset.keys(&clip, FRAME).unwrap().0);
            for &property in preset.extended_properties() {
                let channel: Vec<_> = keys.iter().filter(|k| k.property == property).collect();
                assert!(channel.len() >= 3);
                assert!(channel.windows(2).all(|pair| pair[0].at < pair[1].at));
            }
            let (Command::Batch { commands }, _) = preset.command(&clip, FRAME).unwrap()
                else { panic!("expected a single native command batch") };
            assert_eq!(commands.len(), keys.len());
            let mut authored = clip.clone();
            authored.set_key(preset.extended_properties()[0], 0.5, 0.3, KeyEase::LINEAR);
            assert_eq!(preset.keys(&authored, FRAME).unwrap_err(), PresetError::AlreadyKeyed);
        }
    }

    #[test]
    fn short_fast_motion_samples_each_visible_active_frame() {
        let clip = Clip::blank("c1", "t1", ClipKind::Video, "picture", 0.0, 10.0);
        let controls = PhysicsControls { duration: 0.1, speed: 3.0, ..Default::default() };
        let (keys, _) = PhysicsPreset::SpringZoom.keys_with(&clip, FRAME, controls).unwrap();
        let last = 299.0;
        // The old full-clip grid went straight from frame 0 to frame 5.
        for f in 0..=30 {
            let key = keys.iter().find(|k| (k.at * clip.duration / FRAME - f as f64).abs() < 1e-8).unwrap();
            assert!((key.value - PhysicsPreset::SpringZoom.value_with(&clip,f as f64 / last,controls)).abs() < 1e-10);
        }
        let peak = keys.iter().map(|k| k.value).fold(f64::NEG_INFINITY,f64::max);
        assert!(peak > 1.14);
        assert!(keys.len() <= 242);
    }

}
