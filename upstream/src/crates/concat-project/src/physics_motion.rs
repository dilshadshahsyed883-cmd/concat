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
}

impl PhysicsPreset {
    pub const ALL: [Self; 6] = [
        Self::SpringZoom,
        Self::Pendulum,
        Self::GravityBounce,
        Self::ElasticPop,
        Self::InertiaGlide,
        Self::DampedShake,
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
        }
    }

    fn property(self) -> KeyProperty {
        match self {
            Self::SpringZoom | Self::ElasticPop => KeyProperty::Scale,
            Self::Pendulum | Self::DampedShake => KeyProperty::Rotation,
            Self::GravityBounce => KeyProperty::OffsetY,
            Self::InertiaGlide => KeyProperty::OffsetX,
        }
    }

    /// Sample physically modelled displacement at normalized clip time.
    /// Motion is expressed in the same transform units as the native editor.
    fn value(self, clip: &Clip, t: f64) -> f64 {
        let t = t.clamp(0.0, 1.0);
        match self {
            Self::SpringZoom => {
                // Mass-spring-damper step response: damping ratio 0.45.
                let zeta: f64 = 0.45;
                let natural_frequency: f64 = 18.0;
                let damped = natural_frequency * (1.0 - zeta * zeta).sqrt();
                let response = 1.0 - (-zeta * natural_frequency * t).exp()
                    * ((damped * t).cos()
                        + zeta / (1.0 - zeta * zeta).sqrt() * (damped * t).sin());
                clip.scale * (1.0 + 0.12 * response)
            }
            Self::Pendulum => {
                // Small-angle damped pendulum, with angular drag.
                clip.rotation + 9.0 * (-4.5 * t).exp() * (6.0 * PI * t).cos()
            }
            Self::GravityBounce => {
                // Ballistic fall and restitution at each ground impact.
                // Gravity is chosen so the initial 0.18-frame fall lasts .32.
                let height = 0.18;
                let fall = 0.32;
                let gravity = 2.0 * height / (fall * fall);
                if t < fall {
                    let left = 1.0 - t / fall;
                    return clip.offset_y - height * left * left;
                }
                let mut remaining = t - fall;
                let mut velocity = (2.0 * gravity * height).sqrt() * 0.56;
                for _ in 0..12 {
                    let flight = 2.0 * velocity / gravity;
                    if remaining < flight {
                        let lift = velocity * remaining - 0.5 * gravity * remaining * remaining;
                        return clip.offset_y - lift.max(0.0);
                    }
                    remaining -= flight;
                    velocity *= 0.56; // coefficient of restitution
                }
                clip.offset_y
            }
            Self::ElasticPop => {
                // Underdamped elastic impulse, preserving the initial framing.
                clip.scale * (1.0 + 0.17 * (-5.0 * t).exp() * (19.0 * t).sin())
            }
            Self::InertiaGlide => {
                // Exponential velocity decay with near-zero residual motion.
                clip.offset_x - 0.14 * ((-5.0 * t).exp() - (-5.0_f64).exp())
            }
            Self::DampedShake => {
                // Two modes of a decaying oscillator; deterministic per frame.
                clip.rotation + (-5.5 * t).exp()
                    * (5.0 * (23.0 * t).sin() + 1.5 * (39.0 * t).sin())
            }
        }
    }

    pub fn keys(self, clip: &Clip, frame: f64) -> Result<(Vec<ClipKey>, f64), PresetError> {
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
        // 64 samples maximum, quantized to distinct visible frames. Linear
        // interpolation follows these samples in both desktop and CUDA.
        let steps = (last as usize).min(64);
        let mut keys = Vec::with_capacity(steps + 1);
        for sample in 0..=steps {
            let at_frame = ((sample as f64 * last) / steps as f64).round();
            let t = at_frame / last;
            let value = self.value(clip, t);
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
        let (keys, preview) = self.keys(clip, frame)?;
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
    fn six_presets_generate_finite_frame_aligned_native_keys() {
        for preset in PhysicsPreset::ALL {
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
        for preset in PhysicsPreset::ALL {
            assert_eq!(preset.keys(&single, FRAME).unwrap_err(), PresetError::TooShort);
            let (keys, _) = preset.keys(&pair, FRAME).unwrap();
            assert_eq!(keys.len(), 2);
        }
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
}
