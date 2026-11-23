// SPDX-License-Identifier: AGPL-3.0-or-later
//! Per-preset configured settings. Families retain their native timing units.
use crate::{canvas_motion::CanvasMotionControls, physics_motion::PhysicsControls};
use serde::{Deserialize, Serialize};

/// Persisted by stable catalogue ID in application preferences, never applied by browsing.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    /// Basic/Physics controls (duration is a clip fraction).
    pub physics: PhysicsControls,
    /// Canvas Motion controls (durations are seconds).
    pub canvas: CanvasMotionControls,
    /// Foreground corners owned by Canvas Motion.
    #[serde(default = "default_corners")]
    pub corners: f64,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            physics: PhysicsControls::default(),
            canvas: CanvasMotionControls::default(),
            corners: default_corners(),
        }
    }
}
fn default_corners() -> f64 {
    0.04
}
impl Settings {
    /// Use supported defaults for corrupt or unsupported saved settings.
    pub fn from_value(value: Option<&serde_json::Value>) -> Self {
        value
            .and_then(|v| serde_json::from_value::<Self>(v.clone()).ok())
            .filter(Self::valid)
            .unwrap_or_else(|| Self {
                corners: default_corners(),
                ..Self::default()
            })
    }
    /// Both families' values remain validated independently.
    pub fn valid(&self) -> bool {
        self.physics.valid()
            && self.canvas.valid()
            && self.corners.is_finite()
            && (0.0..=0.2).contains(&self.corners)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn configured_families_round_trip_without_substituting_timing_units() {
        let mut settings = Settings::from_value(None);
        settings.physics.duration = 0.4;
        settings.physics.speed = 2.0;
        settings.canvas.duration_seconds = 7.0;
        settings.canvas.delay_seconds = 1.0;
        settings.corners = 0.1;
        let restored = Settings::from_value(Some(&serde_json::to_value(settings).unwrap()));
        assert_eq!(restored.physics.duration, 0.4);
        assert_eq!(restored.canvas.duration_seconds, 7.0);
        assert_eq!(restored.canvas.delay_seconds, 1.0);
        assert_eq!(restored.corners, 0.1);
        assert_eq!(
            Settings::from_value(Some(&serde_json::json!({"corners":8}))).corners,
            0.04
        );
    }
    #[test]
    fn legacy_canvas_controls_gain_auto_fit_and_peak_defaults() {
        let original = serde_json::to_value(CanvasMotionControls::default()).unwrap();
        let mut legacy = original.as_object().unwrap().clone();
        legacy.remove("middle_scale");
        legacy.remove("peak_time");
        legacy.remove("auto_fit");
        let settings = serde_json::json!({"canvas": legacy});
        let restored = Settings::from_value(Some(&settings));
        assert!(restored.canvas.auto_fit);
        assert_eq!(restored.canvas.middle_scale, 1.0);
        assert_eq!(restored.canvas.peak_time, 0.5);
    }

}
