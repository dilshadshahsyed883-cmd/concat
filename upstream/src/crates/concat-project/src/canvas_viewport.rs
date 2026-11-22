// SPDX-License-Identifier: AGPL-3.0-or-later
//! A fixed Canvas aperture, separate from the content's editable transform.
use crate::model::Clip;
use serde::{Deserialize, Serialize};

/// Extension-map key; old readers keep this metadata while retaining native keys.
pub const KEY: &str = "canvasViewportV1";
/// Fixed frame geometry in timeline fractions. Content scale/X/Y/rotation stay
/// on the ordinary clip, including authored keyframes.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Viewport {
    /// Metadata version.
    pub version: u32,
    /// Contain-fit scale of the unzoomed source inside the frame.
    pub scale: f64,
    /// Frame centre relative to canvas centre.
    pub x: f64,
    /// Frame centre relative to canvas centre.
    pub y: f64,
    /// Actual contain-fit frame opening width.
    pub width: f64,
    /// Actual contain-fit frame opening height.
    pub height: f64,
    /// Clockwise frame rotation, separate from content rotation.
    pub rotation: f64,
    /// Opening corner radius in timeline heights.
    pub radius: f64,
}
impl Viewport {
    /// Valid supported fixed-aperture geometry.
    pub fn valid(&self) -> bool {
        self.version == 1
            && [
                self.scale,
                self.x,
                self.y,
                self.width,
                self.height,
                self.rotation,
                self.radius,
            ]
            .iter()
            .all(|v| v.is_finite())
            && (0.0001..=8.0).contains(&self.scale)
            && (-1.0..=1.0).contains(&self.x)
            && (-1.0..=1.0).contains(&self.y)
            && (0.0001..=1.0).contains(&self.width)
            && (0.0001..=1.0).contains(&self.height)
            && (-180.0..=180.0).contains(&self.rotation)
            && (0.0..=0.5).contains(&self.radius)
    }
    /// Read only validated metadata; malformed extensions are never rendered.
    pub fn of(clip: &Clip) -> Option<Self> {
        serde_json::from_value::<Self>(clip.extra.get(KEY)?.clone())
            .ok()
            .filter(Self::valid)
    }
    /// The engine's resolution-independent composition and aperture contract.
    pub fn engine(self, aspect: f64) -> concat_core::timeline::CanvasFrame {
        concat_core::timeline::CanvasFrame {
            scale: self.scale,
            x: self.x,
            y: self.y,
            width: self.width,
            height: self.height,
            rotation: self.rotation,
            radius: self.radius,
            aspect,
        }
    }
}
