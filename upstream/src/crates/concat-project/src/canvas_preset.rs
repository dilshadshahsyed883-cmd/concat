// SPDX-License-Identifier: AGPL-3.0-or-later
//! Editable visual canvas compositions made from source and shape clips.
//! Legacy headline fields remain in the JSON schema for old projects, but
//! canvas application never generates text or lower-third headline bands.
//! Generated companion layers are owned by one foreground clip and reroll/reapply
//! replaces those companions in one transaction, preserving original media and IDs.
#![allow(missing_docs)]
use crate::commands::{CommandError, IdMint, Outcome};
use crate::model::{
    Background, Clip, ClipKind, ClipMask, MaskShape, Project, ShapeKind, ShapeStyle, TextStyle,
    Track,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::sync::Arc;

#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}
impl Rect {
    fn validate(self) -> bool {
        [self.x, self.y, self.width, self.height]
            .iter()
            .all(|v| v.is_finite())
            && self.x >= 0.0
            && self.y >= 0.0
            && self.width > 0.0
            && self.height > 0.0
            && self.x + self.width <= 1.000001
            && self.y + self.height <= 1.000001
    }
    fn center(self) -> (f64, f64) {
        (
            self.x + self.width / 2.0 - 0.5,
            self.y + self.height / 2.0 - 0.5,
        )
    }
}
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Label {
    pub enabled: bool,
    pub text: String,
    pub rect: Rect,
    pub font_family: String,
    pub font_weight: f64,
    pub font_size: f64,
    pub color: String,
    pub background: String,
    pub opacity: f64,
    pub radius: f64,
    pub padding: f64,
    pub outline: String,
    pub outline_width: f64,
    pub uppercase: bool,
}
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Config {
    pub version: u32,
    pub preset: u32,
    pub foreground: Rect,
    pub radius: f64,
    pub border_color: String,
    pub border_width: f64,
    /// `source`, `blur`, `color`, or `none`.
    pub background: String,
    pub blur: f64,
    pub background_color: String,
    pub background_tint: String,
    pub tint_opacity: f64,
    /// Optional native shape shadow. Defaults preserve old v1 profiles.
    #[serde(default)]
    pub shadow_opacity: f64,
    #[serde(default)]
    pub shadow_offset: f64,
    #[serde(default = "default_shadow_color")]
    pub shadow_color: String,
    /// Extra lower margin of a photo-print mat, as a canvas fraction.
    #[serde(default)]
    pub mat_bottom: f64,
    #[serde(default = "default_mat_color")]
    pub mat_color: String,
    /// Foreground and native decorative shape tilt; legacy profiles keep their source rotation.
    #[serde(default)]
    pub rotation: f64,
    pub headline: Label,
    pub badge: Label,
}
fn default_shadow_color() -> String { "#000000".into() }
fn default_mat_color() -> String { "#ffffff".into() }
fn color(s: &str) -> bool {
    matches!(s.len(), 7 | 9) && s.starts_with('#') && s[1..].bytes().all(|b| b.is_ascii_hexdigit())
}
impl Config {
    pub fn preset(number: u32) -> Option<Self> {
        if !DECODABLE_PRESETS.contains(&number) {
            return None;
        }
        let (border, bg, fg, text, bar, weight, size) = match number {
            1 => (
                "#ff153a",
                "source",
                Rect {
                    x: 0.04,
                    y: 0.035,
                    width: 0.92,
                    height: 0.80,
                },
                "#ffff00",
                "#082fba",
                900.0,
                0.055,
            ),
            2 => (
                "#ff914d",
                "source",
                Rect {
                    x: 0.06,
                    y: 0.035,
                    width: 0.88,
                    height: 0.79,
                },
                "#000000",
                "#ffffff",
                900.0,
                0.052,
            ),
            _ => (
                "#00000000",
                "blur",
                Rect {
                    x: 0.025,
                    y: 0.015,
                    width: 0.95,
                    height: 0.90,
                },
                "#000000",
                "#ffe532",
                400.0,
                0.027,
            ),
        };
        let headline = Label {
            // Lower-thirds belong exclusively to Text -> Captions, not Canvas.
            enabled: false,
            text: String::new(),
            rect: if number == 3 {
                Rect {
                    x: 0.035,
                    y: 0.925,
                    width: 0.93,
                    height: 0.055,
                }
            } else {
                Rect {
                    x: 0.035,
                    y: 0.84,
                    width: 0.93,
                    height: 0.135,
                }
            },
            font_family: if number == 3 {
                "Hanken Grotesk"
            } else {
                "Inter"
            }
            .into(),
            font_weight: weight,
            font_size: size,
            color: text.into(),
            background: bar.into(),
            opacity: 1.0,
            radius: if number == 1 { 0.0 } else { 0.014 },
            padding: 0.012,
            outline: "#000000".into(),
            outline_width: if number == 1 { 0.003 } else { 0.0 },
            uppercase: number != 3,
        };
        let badge = Label {
            enabled: false,
            text: if number == 2 { "SUBSCRIBE" } else { "LIKE" }.into(),
            rect: if number == 2 {
                Rect {
                    x: 0.035,
                    y: 0.04,
                    width: 0.16,
                    height: 0.055,
                }
            } else {
                Rect {
                    x: 0.80,
                    y: 0.04,
                    width: 0.16,
                    height: 0.055,
                }
            },
            font_family: "Inter".into(),
            font_weight: 700.0,
            font_size: 0.022,
            color: "#ffffff".into(),
            background: "#ff1838".into(),
            opacity: 1.0,
            radius: 0.025,
            padding: 0.006,
            outline: "#000000".into(),
            outline_width: 0.0,
            uppercase: true,
        };
        let mut config = Self {
            version: 1,
            preset: number,
            foreground: fg,
            radius: 0.0,
            border_color: border.into(),
            border_width: if number == 1 {
                0.006
            } else if number == 2 {
                0.018
            } else {
                0.0
            },
            background: bg.into(),
            blur: if number == 3 { 0.75 } else { 0.0 },
            background_color: "#000000".into(),
            background_tint: "#000000".into(),
            tint_opacity: 0.0,
            shadow_opacity: 0.0,
            shadow_offset: 0.0,
            shadow_color: default_shadow_color(),
            mat_bottom: 0.0,
            mat_color: default_mat_color(),
            rotation: 0.0,
            headline,
            badge,
        };
        // ID 3 already is Adaptive Blur: do not introduce a duplicate preset.
        // IDs 1 and 2 stay untouched legacy styles, regardless of their colors.
        match number {
            4 => { // Gallery Mat: centered picture with balanced neutral margins.
                config.foreground = Rect { x: 0.09, y: 0.12, width: 0.82, height: 0.76 };
                config.background = "color".into();
                config.background_color = "#e4e0d6".into();
                config.border_color = "#f8f7f2".into();
                config.border_width = 0.008;
            }
            5 => { // Floating Card: elevated rounded source above a colored canvas.
                config.foreground = Rect { x: 0.08, y: 0.10, width: 0.84, height: 0.76 };
                config.background = "color".into();
                config.background_color = "#152336".into();
                config.radius = 0.025;
                config.shadow_opacity = 0.38;
                config.shadow_offset = 0.018;
            }
            6 => { // Photo Print: a genuine larger lower margin and tilted backing.
                config.foreground = Rect { x: 0.10, y: 0.08, width: 0.80, height: 0.70 };
                config.background = "color".into();
                config.background_color = "#25303a".into();
                config.mat_bottom = 0.10;
                config.rotation = -3.0;
            }
            7 => { // Cinema Frame: matte/letterbox space without added caption bars.
                config.foreground = Rect { x: 0.025, y: 0.145, width: 0.95, height: 0.71 };
                config.background = "color".into();
                config.background_color = "#050505".into();
            }
            8 => { // Editorial Panel: reserved negative space, no generated title.
                config.foreground = Rect { x: 0.38, y: 0.08, width: 0.56, height: 0.84 };
                config.background = "color".into();
                config.background_color = "#f1e7dc".into();
            }
            9 => { // Broadcast Edge: asymmetric native graphic panels.
                config.foreground = Rect { x: 0.19, y: 0.10, width: 0.76, height: 0.79 };
                config.background = "color".into();
                config.background_color = "#0b182b".into();
                config.border_color = "#e8edf5".into();
                config.border_width = 0.006;
            }
            12 => { // Focus Island: sharp source over a dark same-source fill.
                config.foreground = Rect { x: 0.12, y: 0.12, width: 0.76, height: 0.76 };
                config.background = "source".into();
                config.background_tint = "#07101c".into();
                config.tint_opacity = 0.68;
                config.radius = 0.018;
                config.shadow_opacity = 0.3;
                config.shadow_offset = 0.012;
            }
            13 => { // Diagonal Cut: angled background graphic edges.
                config.foreground = Rect { x: 0.12, y: 0.10, width: 0.76, height: 0.80 };
                config.background = "color".into();
                config.background_color = "#111827".into();
                config.border_color = "#f1f5f9".into();
                config.border_width = 0.004;
            }
            14 => { // Projector Slide: a mounted photographic transparency.
                config.foreground = Rect { x: 0.18, y: 0.18, width: 0.64, height: 0.64 };
                config.background = "color".into();
                config.background_color = "#2c2923".into();
                config.mat_color = "#ddd2b9".into();
                config.border_color = "#514638".into();
                config.border_width = 0.006;
            }
            15 => { // Browser Window: a picture under a native window header.
                config.foreground = Rect { x: 0.10, y: 0.22, width: 0.80, height: 0.66 };
                config.background = "color".into();
                config.background_color = "#182437".into();
                config.radius = 0.008;
                config.border_color = "#cbd5e1".into();
                config.border_width = 0.004;
            }
            16 => { // Taped Memory: an inset photo with decorative tape overlays.
                config.foreground = Rect { x: 0.15, y: 0.13, width: 0.70, height: 0.68 };
                config.background = "color".into();
                config.background_color = "#d6c5aa".into();
                config.border_color = "#fff9ed".into();
                config.border_width = 0.014;
                config.shadow_opacity = 0.22;
                config.shadow_offset = 0.012;
                config.rotation = -3.0;
            }
            18 => { // Broken Grid: uninterrupted foreground over staggered blocks.
                config.foreground = Rect { x: 0.13, y: 0.13, width: 0.74, height: 0.74 };
                config.background = "color".into();
                config.background_color = "#18232b".into();
                config.border_color = "#d8e3dc".into();
                config.border_width = 0.004;
            }
            _ => {}
        }
        Some(config)
    }
    pub fn validate(&self) -> Result<(), String> {
        if self.version != 1 || !DECODABLE_PRESETS.contains(&self.preset) || !self.foreground.validate() {
            return Err("Unsupported canvas version/preset or unsafe foreground bounds".into());
        }
        if !matches!(
            self.background.as_str(),
            "source" | "blur" | "color" | "none"
        ) {
            return Err("Unknown canvas background mode".into());
        }
        for (name, value, max) in [
            ("radius", self.radius, 0.5),
            ("borderWidth", self.border_width, 0.1),
            ("blur", self.blur, 1.0),
            ("tintOpacity", self.tint_opacity, 1.0),
            ("shadowOpacity", self.shadow_opacity, 1.0),
            ("shadowOffset", self.shadow_offset, 0.08),
            ("matBottom", self.mat_bottom, 0.2),
        ] {
            if !value.is_finite() || value < 0.0 || value > max {
                return Err(format!("Invalid canvas {name}"));
            }
        }
        if !self.rotation.is_finite() || self.rotation.abs() > 12.0 {
            return Err("Invalid canvas rotation (max 12 degrees)".into());
        }
        for value in [
            &self.border_color,
            &self.background_color,
            &self.background_tint,
            &self.shadow_color,
            &self.mat_color,
        ] {
            if !color(value) {
                return Err("Canvas colors must be #rrggbb[aa]".into());
            }
        }
        for label in [&self.headline, &self.badge] {
            if !label.rect.validate()
                || label.text.len() > 8192
                || label.font_family.trim().is_empty()
                || !label.font_weight.is_finite()
                || !(100.0..=900.0).contains(&label.font_weight)
                || !label.font_size.is_finite()
                || !(0.01..=0.2).contains(&label.font_size)
                || !label.opacity.is_finite()
                || !(0.0..=1.0).contains(&label.opacity)
                || !label.radius.is_finite()
                || !(0.0..=0.2).contains(&label.radius)
                || !label.padding.is_finite()
                || !(0.0..=0.1).contains(&label.padding)
                || !label.outline_width.is_finite()
                || !(0.0..=0.029).contains(&label.outline_width)
                || [&label.color, &label.background, &label.outline]
                    .iter()
                    .any(|s| !color(s))
            {
                return Err("Invalid canvas headline/badge controls or unsafe label bounds".into());
            }
        }
        Ok(())
    }

    /// Percentage of the selected preset's own baseline picture aperture.
    /// Independent Width/Height edits remain possible; area is the neutral
    /// proportional-size readout for such an edited rectangle.
    pub fn picture_size_percent(&self) -> f64 {
        let Some(factory) = Self::preset(self.preset) else { return 100.0 };
        let ratio = self.foreground.width * self.foreground.height
            / (factory.foreground.width * factory.foreground.height);
        100.0 * ratio.sqrt()
    }

    /// Scale the existing picture aperture proportionally around its center.
    /// Unlike Content Zoom/Crop this changes the foreground rectangle itself;
    /// companion border, shadow and mat are still generated from that rectangle.
    /// Invalid/off-canvas geometry is clamped before it can reach a renderer.
    pub fn set_picture_size(&mut self, percent: f64) -> Result<(), String> {
        if !percent.is_finite() || !(40.0..=130.0).contains(&percent) {
            return Err("Picture size must be within 40–130%".into());
        }
        let previous = self.foreground;
        let current = self.picture_size_percent();
        if !current.is_finite() || current <= 0.0 {
            return Err("Invalid current picture geometry".into());
        }
        let cx = previous.x + previous.width / 2.0;
        let cy = previous.y + previous.height / 2.0;
        let extent = self.border_width
            + if self.shadow_opacity > 0.0 { self.shadow_offset } else { 0.0 };
        // Reserve decorative spill on all sides; Photo Print also extends
        // downward beyond the aperture. Do not move the picture's center.
        let w_room = 2.0 * (cx - extent).min(1.0 - cx - extent);
        let h_room = 2.0 * (cy - extent)
            .min(1.0 - cy - extent - self.mat_bottom);
        let max_factor = (w_room / previous.width)
            .min(h_room / previous.height).max(0.0);
        let factor = (percent / current).min(max_factor);
        if !factor.is_finite() || factor <= 0.0
            || previous.width * factor < 0.05
            || previous.height * factor < 0.05
        {
            return Err("Picture size would leave insufficient canvas space".into());
        }
        self.foreground = Rect {
            x: cx - previous.width * factor / 2.0,
            y: cy - previous.height * factor / 2.0,
            width: previous.width * factor,
            height: previous.height * factor,
        };
        if let Err(error) = self.validate() {
            self.foreground = previous;
            return Err(error);
        }
        Ok(())
    }
    pub fn from_json(source: &str) -> Result<Self, String> {
        if source.len() > 65536 {
            return Err("Canvas profile exceeds 64 KiB".into());
        }
        let spec: Self = serde_json::from_str(source).map_err(|e| e.to_string())?;
        spec.validate()?;
        Ok(spec)
    }
}
fn error(reason: impl Into<String>) -> CommandError {
    CommandError::UnsafeEdit {
        reason: reason.into(),
    }
}
fn tagged(mut clip: Clip, owner: &str, role: &str) -> Arc<Clip> {
    clip.extra.insert("canvasOwner".into(), json!(owner));
    clip.extra.insert("canvasRole".into(), json!(role));
    Arc::new(clip)
}
fn track(
    project: &mut Project,
    mint: &mut IdMint,
    owner_track: &str,
    role: &str,
    before: bool,
) -> String {
    if let Some(t) = project.active().tracks.iter().find(|t| {
        t.extra.get("canvasTrack") == Some(&json!(owner_track))
            && t.extra.get("canvasRole") == Some(&json!(role))
    }) {
        return t.id.clone();
    }
    let id = mint.next("t");
    let mut t = Track {
        id: id.clone(),
        visible: true,
        muted: true,
        extra: Default::default(),
    };
    t.extra.insert("canvasTrack".into(), json!(owner_track));
    t.extra.insert("canvasRole".into(), json!(role));
    let tl = project.active_mut();
    let i = tl
        .tracks
        .iter()
        .position(|t| t.id == owner_track)
        .unwrap_or(0);
    if before {
        tl.tracks.insert(i, t);
    } else {
        let after = tl
            .tracks
            .iter()
            .enumerate()
            .filter(|(_, t)| t.extra.get("canvasTrack") == Some(&json!(owner_track)))
            .map(|(i, _)| i + 1)
            .max()
            .unwrap_or(i + 1)
            .max(i + 1);
        tl.tracks.insert(after, t);
    }
    id
}
fn rectangle(
    mint: &mut IdMint,
    owner: &Clip,
    lane: &str,
    role: &str,
    rect: Rect,
    fill: &str,
    radius: f64,
    opacity: f64,
) -> Arc<Clip> {
    let mut c = Clip::blank(
        mint.next("c"),
        lane,
        ClipKind::Shape,
        format!("Canvas {role}"),
        owner.start,
        owner.duration,
    );
    let (x, y) = rect.center();
    c.offset_x = x;
    c.offset_y = y;
    c.opacity = opacity;
    c.shape = Some(ShapeStyle {
        kind: ShapeKind::Square,
        width: rect.width,
        height: rect.height,
        corner_radius: radius,
        fill: fill.into(),
        stroke: String::new(),
        stroke_width: 0.0,
        ..ShapeStyle::default()
    });
    tagged(c, &owner.id, role)
}
fn label(
    mint: &mut IdMint,
    owner: &Clip,
    lane: &str,
    role: &str,
    config: &Label,
) -> Vec<Arc<Clip>> {
    if !config.enabled {
        return vec![];
    }
    let mut result = vec![rectangle(
        mint,
        owner,
        lane,
        role,
        config.rect,
        &config.background,
        config.radius,
        config.opacity,
    )];
    if !config.text.trim().is_empty() {
        let mut c = Clip::blank(
            mint.next("c"),
            lane,
            ClipKind::Text,
            format!("Canvas {role} text"),
            owner.start,
            owner.duration,
        );
        let (x, y) = config.rect.center();
        c.offset_x = x;
        c.offset_y = y;
        c.text = Some(TextStyle {
            content: if config.uppercase {
                config.text.to_uppercase()
            } else {
                config.text.clone()
            },
            font_family: config.font_family.clone(),
            font_size: config.font_size,
            font_weight: config.font_weight,
            color: config.color.clone(),
            stroke_color: config.outline.clone(),
            stroke_width: config.outline_width,
            shadow: false,
            opacity: config.opacity,
            max_width: (config.rect.width - 2.0 * config.padding).max(0.01),
            max_height: (config.rect.height - 2.0 * config.padding).max(0.01),
            ..TextStyle::default()
        });
        result.push(tagged(c, &owner.id, &format!("{role}Text")));
    }
    result
}
/// Compare a legacy lower-third with the exact shape that an older Canvas
/// preset generated. A shape the user edited is NOT safe to discard on reapply.
/// Real Text captions are never canvas-owned and are not considered here.
fn unedited_legacy_headline(piece: &Clip, owner: &Clip) -> bool {
    if piece.extra.get("canvasRole") != Some(&json!("headline")) {
        return false;
    }
    let Some(saved) = owner.extra.get("canvasPreset") else {
        return false;
    };
    let Ok(previous) = serde_json::from_value::<Config>(saved.clone()) else {
        return false;
    };
    // A user-authored headline profile (color, positioning, font or text)
    // is customized even when its generated rectangle was never edited.
    // Only the exact historical default placeholder is safe to remove.
    let Some(mut default) = Config::preset(previous.preset) else {
        return false;
    };
    default.headline.enabled = true;
    if previous.headline != default.headline {
        return false;
    }
    // Rebuild the old rectangle without consuming the project's real IdMint.
    let mut test_mint = IdMint::default();
    let mut expected = rectangle(
        &mut test_mint,
        owner,
        &piece.track_id,
        "headline",
        previous.headline.rect,
        &previous.headline.background,
        previous.headline.radius,
        previous.headline.opacity,
    );
    Arc::make_mut(&mut expected).id = piece.id.clone();
    serde_json::to_value(piece).ok() == serde_json::to_value(expected.as_ref()).ok()
}
// Generated companions retain a canonical snapshot so later manual edits can be
// distinguished from normal lifecycle changes. Identity is kept outside it.
const COMPANION_SNAPSHOT: &str = "canvasGeneratedV1";
const FOREGROUND_MASK: &str = "canvasGeneratedMaskV1";

fn companion_value(clip: &Clip) -> Value {
    let mut value = serde_json::to_value(clip).unwrap_or(Value::Null);
    if let Some(map) = value.as_object_mut() {
        for key in [COMPANION_SNAPSHOT, "id", "canvasOwner"] { map.remove(key); }
    }
    value
}
pub(crate) fn untouched_companion(clip: &Clip) -> bool {
    clip.extra.get(COMPANION_SNAPSHOT) == Some(&companion_value(clip))
}
pub(crate) fn stamp_companion(clip: &mut Clip) {
    *clip = clip.clone().tidy();
    clip.extra.insert(COMPANION_SNAPSHOT.into(), companion_value(clip));
}

/// Follow an owner's normal timing edits, and remove companions with deleted
/// owners, in the same command/undo step. Explicitly edited companion styling
/// stays authored; only unchanged generated snapshots are refreshed.
pub(crate) fn reconcile_companions(before: &Project, after: &mut Project) {
    use std::collections::HashMap;
    for timeline in &mut after.timelines {
        let Some(old) = before.timelines.iter().find(|old| old.id == timeline.id) else { continue; };
        let owners: HashMap<_,_> = timeline.clips.iter()
            .filter(|clip| !clip.extra.contains_key("canvasOwner"))
            .map(|clip| (clip.id.clone(),clip.clone())).collect();
        let timeline = Arc::make_mut(timeline);
        timeline.clips.retain(|piece| {
            let Some(owner) = piece.extra.get("canvasOwner").and_then(Value::as_str) else { return true; };
            !(old.clip(owner).is_some() && !owners.contains_key(owner))
        });
        for piece in &mut timeline.clips {
            let Some(owner_id) = piece.extra.get("canvasOwner").and_then(Value::as_str) else { continue; };
            let (Some(owner),Some(previous)) = (owners.get(owner_id),old.clip(owner_id)) else { continue; };
            let owner_changed = owner.start != previous.start || owner.duration != previous.duration
                || owner.source_start != previous.source_start || owner.speed != previous.speed
                || owner.speed_curve != previous.speed_curve;
            if !owner_changed { continue; }
            let backdrop = piece.extra.get("canvasRole").and_then(Value::as_str) == Some("backdrop");
            if piece.start == owner.start && piece.duration == owner.duration
                && (!backdrop || (piece.source_start == owner.source_start && piece.speed == owner.speed
                    && piece.speed_curve == owner.speed_curve)) { continue; }
            let untouched = untouched_companion(piece);
            let piece = Arc::make_mut(piece);
            if piece.duration != owner.duration {
                let from = if previous.speed == owner.speed {
                    (owner.source_start - previous.source_start) / previous.speed
                } else { 0.0 };
                piece.rewindow_keys(piece.duration,from,from+owner.duration);
                piece.rewindow_caption(from,from+owner.duration);
            }
            piece.start = owner.start;
            piece.duration = owner.duration;
            if backdrop {
                piece.source_start = owner.source_start;
                piece.speed = owner.speed;
                piece.speed_curve = owner.speed_curve.clone();
            }
            if untouched { stamp_companion(piece); }
        }
    }
}

/// True if an edit changed clips on one of the protected tracks. Track IDs
/// are timeline-local; selecting another timeline is not a clip modification.
pub fn protected_tracks_changed(before: &Project, after: &Project, timeline_id: &str, locked: &[String]) -> bool {
    let Some(old) = before.timelines.iter().find(|t| t.id == timeline_id) else { return false; };
    let new = after.timelines.iter().find(|t| t.id == timeline_id);
    locked.iter().any(|track| {
        let mut a: Vec<_> = old.clips.iter().filter(|c| &c.track_id == track).collect();
        let mut b: Vec<_> = new.into_iter().flat_map(|t| &t.clips).filter(|c| &c.track_id == track).collect();
        a.sort_by(|a,b|a.id.cmp(&b.id)); b.sort_by(|a,b|a.id.cmp(&b.id));
        a != b
    })
}

#[derive(Default, Debug)]
pub struct Conflicts {
    pub masks: Vec<String>,
    pub customized_layers: Vec<String>,
}
impl Conflicts {
    pub fn is_empty(&self) -> bool { self.masks.is_empty() && self.customized_layers.is_empty() }
}

fn authored_mask(project: &Project, clip: &Clip) -> bool {
    let Some(mask) = clip.mask else { return false; };
    if crate::motion_preset::generated_ownership(clip).and_then(|record|record.style)
        .is_some_and(|style|style.owns_mask && style.applied_mask==clip.mask) {return false;}

    if let Some(saved) = clip.extra.get(FOREGROUND_MASK) {
        return saved != &json!(Some(mask));
    }
    // Read legacy preset-owned masks conservatively, using the saved layout
    // geometry rather than the current foreground transform or old base mask.
    let previous = clip.extra.get("canvasPreset").cloned()
        .and_then(|value| serde_json::from_value::<Config>(value).ok());
    if let Some(previous) = previous {
        if let Some(media) = project.media_by_id(&clip.media_id) {
            let (sw,sh) = (media.width.unwrap_or(0) as f64,media.height.unwrap_or(0) as f64);
            let video = project.active().video;
            if sw > 0.0 && sh > 0.0 && previous.radius > 0.0 {
                let short = sw.min(sh);
                let fit = (video.width as f64 / sw).min(video.height as f64 / sh);
                let scale = (previous.foreground.width * video.width as f64 / (sw*fit))
                    .min(previous.foreground.height * video.height as f64 / (sh*fit));
                let mut expected = ClipMask::of(MaskShape::Rectangle);
                expected.width=sw/short; expected.height=sh/short; expected.feather=0.0;
                expected.roundness=(previous.radius * video.height as f64 / scale / (short*fit/2.0)).clamp(0.0,1.0);
                return mask != expected.tidy();
            }
        }
    }
    true
}

/// Inspect the actual current mask and owned layers without mutating anything.
pub fn conflicts(project: &Project, targets: &[String]) -> Conflicts {
    let mut result = Conflicts::default();
    for id in targets {
        let Some(owner) = project.active().clip(id) else { continue; };
        if authored_mask(project,owner) { result.masks.push(owner.name.clone()); }
        for piece in &project.active().clips {
            if piece.extra.get("canvasOwner") == Some(&json!(id))
                && !untouched_companion(piece) && !unedited_legacy_headline(piece,owner) {
                result.customized_layers.push(piece.name.clone());
            }
        }
    }
    result
}

/// Apply to explicit unlocked visual targets. No media IO or source mutation.
pub fn apply(
    project: &mut Project,
    mint: &mut IdMint,
    config: Config,
    targets: Vec<String>,
    locked: Vec<String>,
) -> Result<Outcome, CommandError> {
    apply_with_confirmation(project,mint,config,targets,locked,false)
}

/// Explicitly confirmed replacement is still transactional and lock-protected.
pub fn apply_with_confirmation(project: &mut Project, mint: &mut IdMint, config: Config,
    targets: Vec<String>, locked: Vec<String>, replace_conflicts: bool) -> Result<Outcome, CommandError> {
    config.validate().map_err(error)?;
    let conflicts = conflicts(project,&targets);
    if !replace_conflicts && !conflicts.is_empty() {
        return Err(error("Canvas conflicts with an authored mask or customized layer; confirm replacement or cancel"));
    }
    if targets.is_empty() {
        return Err(error("Select at least one image/video canvas target"));
    }
    let mut staged = project.clone();
    let mut created = None;
    for id in targets {
        let original = staged
            .active()
            .clip(&id)
            .cloned()
            .ok_or_else(|| error("Canvas target no longer exists"))?;
        if !original.kind.is_visual() || original.extra.contains_key("canvasOwner") {
            return Err(error("Canvas targets must be original image/video clips"));
        }
        if locked.contains(&original.track_id) {
            return Err(error("Canvas target is on a locked track"));
        }
        if staged.active().clips.iter().any(|piece|
            piece.extra.get("canvasOwner") == Some(&json!(&id)) && locked.contains(&piece.track_id)) {
            return Err(error("Canvas companion is on a locked track; unlock it before replacing the layout"));
        }
        // Canvas Preset wins over generated Canvas Animation, but never
        // over Basic/Physics or manually authored keyframes. Stage the clear
        // before creating the restoration snapshot or cloning companions.
        let original = {
            let clip = staged.active_mut().clip_mut(&id).ok_or(CommandError::MediaGone)?;
            crate::motion_preset::clear_canvas_animation_for_preset(clip)?;
            clip.clone()
        };
        let media = staged
            .media_by_id(&original.media_id)
            .ok_or(CommandError::MediaGone)?;
        let (sw, sh) = (
            media.width.unwrap_or(0) as f64,
            media.height.unwrap_or(0) as f64,
        );
        if sw <= 0.0 || sh <= 0.0 {
            return Err(error(
                "Probe media dimensions before applying canvas presets",
            ));
        }
        let video = staged.active().video;
        let (w, h) = (video.width as f64, video.height as f64);
        let short = sw.min(sh);
        if sw / short > 4.0 || sh / short > 4.0 {
            return Err(error("Canvas mask supports source aspect ratios up to 4:1"));
        }
        let fit = (w / sw).min(h / sh);
        let fw = sw * fit;
        let fh = sh * fit;
        let scale = (config.foreground.width * w / fw).min(config.foreground.height * h / fh);
        let actual = Rect {
            x: config.foreground.x + (config.foreground.width - fw * scale / w) / 2.0,
            y: config.foreground.y + (config.foreground.height - fh * scale / h) / 2.0,
            width: fw * scale / w,
            height: fh * scale / h,
        };
        let border_w = config.border_width * h / w;
        if actual.x < border_w
            || actual.y < config.border_width
            || actual.x + actual.width + border_w > 1.000001
            || actual.y + actual.height + config.border_width > 1.000001
        {
            return Err(error(
                "Border exceeds safe canvas bounds; reduce inset size or border thickness",
            ));
        }
        // Keep an immutable pre-preset snapshot for safe reapplication/removal.
        let baseline = original
            .extra
            .get("canvasOriginal")
            .cloned()
            .unwrap_or_else(|| serde_json::to_value(&original).unwrap_or(Value::Null));
        let mut base: Clip = serde_json::from_value(baseline.clone())
            .map_err(|_| error("Canvas original snapshot is invalid"))?;
        // Older versions could store Canvas Animation in the restoration
        // snapshot. Do not resurrect an exactly owned, overwritten animation.
        let baseline = if !crate::motion_preset::edited_canvas_animation(&base)
            && crate::motion_preset::clear_canvas_animation_for_preset(&mut base)?
        {
            serde_json::to_value(&base).map_err(|e| error(e.to_string()))?
        } else { baseline };
        staged
            .active_mut()
            .clips
            .retain(|c| c.extra.get("canvasOwner") != Some(&json!(&id)));
        let mut layers = Vec::new();
        if config.background != "none" {
            let bg_lane = track(&mut staged, mint, &original.track_id, "backdrop", true);
            let mut bg = base.clone();
            // Restoration metadata is immutable; playback state is current.
            bg.start = original.start;
            bg.duration = original.duration;
            bg.source_start = original.source_start;
            bg.speed = original.speed;
            bg.speed_curve = original.speed_curve.clone();
            bg.id = mint.next("c");
            bg.track_id = bg_lane;
            bg.name = "Canvas source background".into();
            bg.volume = 0.0;
            bg.muted = Some(true);
            bg.detached_from = None;
            bg.transition_in = None;
            bg.keys.clear();
            bg.scale = 1.0;
            bg.opacity = 1.0;
            bg.offset_x = 0.0;
            bg.offset_y = 0.0;
            bg.rotation = 0.0;
            bg.video_effects.clear();
            let mut mask = ClipMask::of(MaskShape::Rectangle);
            mask.width = 4.0;
            mask.height = 4.0;
            mask.roundness = 0.0;
            mask.feather = 0.0;
            mask.invert = true;
            bg.mask = Some(mask);
            bg.background = Some(if config.background == "color" {
                Background::Color {
                    color: config.background_color.clone(),
                }
            } else {
                Background::Blur {
                    amount: if config.background == "source" {
                        0.0
                    } else {
                        config.blur
                    },
                }
            });
            bg.extra.clear();
            layers.push(tagged(bg, &id, "backdrop"));
        }
        if config.tint_opacity > 0.0 {
            let tint_lane = track(&mut staged, mint, &original.track_id, "tint", true);
            layers.push(rectangle(
                mint,
                &original,
                &tint_lane,
                "tint",
                Rect {
                    x: 0.0,
                    y: 0.0,
                    width: 1.0,
                    height: 1.0,
                },
                &config.background_tint,
                0.0,
                config.tint_opacity,
            ));
        }
        // Each composition owns its own decorative native shape lanes. The
        // foreground remains one uninterrupted original source. Background
        // decoration is independent of editable content transforms.
        // Clip geometry to the canvas so extreme user aperture edits do not
        // manufacture invalid or zero-sized shape layers.
        let mut decorate = |role: &str, before: bool, rect: Rect, fill: &str,
                            radius: f64, opacity: f64, rotation: f64| {
            let x = rect.x.max(0.0);
            let y = rect.y.max(0.0);
            let right = (rect.x + rect.width).min(1.0);
            let bottom = (rect.y + rect.height).min(1.0);
            if right - x < 0.005 || bottom - y < 0.005 { return; }
            let lane = track(&mut staged, mint, &original.track_id, role, before);
            let mut piece = rectangle(mint, &original, &lane, role,
                Rect { x, y, width: right - x, height: bottom - y },
                fill, radius, opacity);
            Arc::make_mut(&mut piece).rotation = rotation;
            layers.push(piece);
        };
        let rect = |x, y, width, height| Rect { x, y, width, height };
        match config.preset {
            9 => {
                decorate("broadcastSide", true, rect(0.0, 0.0, 0.18, 1.0), "#19395b", 0.0, 1.0, 0.0);
                decorate("broadcastAccent", true, rect(0.025, 0.09, 0.018, 0.66), "#f33748", 0.0, 1.0, 0.0);
                decorate("broadcastFoot", true, rect(0.05, 0.92, 0.88, 0.045), "#315d87", 0.0, 1.0, 0.0);
                decorate("broadcastTop", true, rect(0.60, 0.025, 0.36, 0.035), "#f33748", 0.0, 1.0, 0.0);
            }
            13 => {
                decorate("diagonalLeft", true, rect(0.0, 0.0, 0.32, 1.0), "#263d66", 0.0, 1.0, 18.0);
                decorate("diagonalRight", true, rect(0.74, 0.0, 0.26, 1.0), "#943c56", 0.0, 1.0, 18.0);
                decorate("diagonalHighlight", true, rect(0.09, 0.0, 0.025, 1.0), "#efb454", 0.0, 1.0, 18.0);
            }
            14 => {
                decorate("projectorMount", true,
                    rect(actual.x - 0.075, actual.y - 0.075, actual.width + 0.15, actual.height + 0.15),
                    &config.mat_color, 0.012, 1.0, config.rotation);
                decorate("projectorRail", true,
                    rect(actual.x - 0.055, actual.y + actual.height + 0.035, actual.width + 0.11, 0.012),
                    "#8e8068", 0.002, 1.0, 0.0);
            }
            15 => {
                let header = actual.y.min(0.075);
                decorate("browserShell", true,
                    rect(actual.x - 0.018, actual.y - header, actual.width + 0.036, actual.height + header + 0.018),
                    "#d9e2ee", 0.012, 1.0, 0.0);
                decorate("browserHeader", true,
                    rect(actual.x - 0.018, actual.y - header, actual.width + 0.036, header),
                    "#34465e", 0.007, 1.0, 0.0);
                let dot_h = (header * 0.24).min(0.018);
                let dot_w = dot_h * h / w;
                if dot_h >= 0.005 {
                    for (i, (role, fill)) in [("browserDotRed", "#f87171"), ("browserDotAmber", "#fbbf24"), ("browserDotGreen", "#4ade80")].into_iter().enumerate() {
                        decorate(role, true, rect(actual.x + i as f64 * dot_w * 1.8, actual.y - header * 0.62, dot_w, dot_h),
                            fill, dot_h, 1.0, 0.0);
                    }
                }
            }
            16 => {
                let tape_w = (actual.width * 0.32).min(0.18);
                let tape_h = (actual.height * 0.13).min(0.065);
                // Tape sits over the print corners and is part of the same
                // owned composition, not a second media source.
                let (cx, cy) = actual.center();
                let angle = config.rotation.to_radians();
                let tape_center = |x: f64, y: f64| {
                    let dx = (x - 0.5 - cx) * w / h;
                    let dy = y - 0.5 - cy;
                    (0.5 + cx + (dx * angle.cos() - dy * angle.sin()) * h / w,
                     0.5 + cy + dx * angle.sin() + dy * angle.cos())
                };
                let (lx, ly) = tape_center(actual.x + tape_w * 0.30, actual.y + tape_h * 0.14);
                let (rx, ry) = tape_center(actual.x + actual.width - tape_w * 0.30, actual.y + tape_h * 0.14);
                decorate("tapeLeft", false, rect(lx - tape_w / 2.0, ly - tape_h / 2.0, tape_w, tape_h),
                    "#f4e1ad", 0.002, 0.88, config.rotation - 16.0);
                decorate("tapeRight", false, rect(rx - tape_w / 2.0, ry - tape_h / 2.0, tape_w, tape_h),
                    "#f4e1ad", 0.002, 0.88, config.rotation + 16.0);
            }
            18 => {
                decorate("gridUpper", true, rect(0.025, 0.025, 0.56, 0.22), "#355f61", 0.0, 1.0, 0.0);
                decorate("gridRight", true, rect(0.72, 0.035, 0.25, 0.55), "#c67c51", 0.0, 1.0, 0.0);
                decorate("gridLower", true, rect(0.35, 0.75, 0.62, 0.22), "#546e89", 0.0, 1.0, 0.0);
                decorate("gridLeft", true, rect(0.035, 0.44, 0.24, 0.52), "#a5a77b", 0.0, 1.0, 0.0);
            }
            _ => {}
        }
        drop(decorate);
        // Native decorative shapes stay tied to this same source and are
        // replaced atomically on reapply. No separate media slot is created.
        if config.shadow_opacity > 0.0 {
            let offset = config.shadow_offset;
            let shadow_rect = Rect {
                x: actual.x + offset, y: actual.y + offset,
                width: actual.width, height: actual.height,
            };
            if !shadow_rect.validate() {
                return Err(error("Shadow exceeds canvas; reduce offset or media size"));
            }
            let shadow_lane = track(&mut staged, mint, &original.track_id, "shadow", true);
            let mut shadow = rectangle(mint, &original, &shadow_lane, "shadow",
                shadow_rect, &config.shadow_color, config.radius, config.shadow_opacity);
            Arc::make_mut(&mut shadow).rotation = config.rotation;
            layers.push(shadow);
        }
        if config.mat_bottom > 0.0 {
            let margin = 0.02;
            let mat_rect = Rect {
                x: actual.x - margin, y: actual.y - margin,
                width: actual.width + 2.0 * margin,
                height: actual.height + margin + config.mat_bottom,
            };
            if !mat_rect.validate() {
                return Err(error("Photo mat exceeds canvas; reduce mat bottom or foreground size"));
            }
            let mat_lane = track(&mut staged, mint, &original.track_id, "mat", true);
            let mut mat = rectangle(mint, &original, &mat_lane, "mat",
                mat_rect, &config.mat_color, config.radius, 1.0);
            Arc::make_mut(&mut mat).rotation = config.rotation;
            layers.push(mat);
        }
        if config.border_width > 0.0 {
            let frame_lane = track(&mut staged, mint, &original.track_id, "frame", true);
            let mut frame = rectangle(
                mint,
                &original,
                &frame_lane,
                "frame",
                Rect {
                    x: actual.x - border_w,
                    y: actual.y - config.border_width,
                    width: actual.width + 2.0 * border_w,
                    height: actual.height + 2.0 * config.border_width,
                },
                &config.border_color,
                config.radius + config.border_width,
                1.0,
            );
            Arc::make_mut(&mut frame).rotation = config.rotation;
            layers.push(frame);
        }
        // Canvas never generates a headline bar, fake caption text or even
        // an empty headline track. The legacy config.headline JSON field is
        // intentionally read but is not a rendering instruction.
        // Optional stickers remain separate and disabled by default.
        if config.badge.enabled {
            let badge_bg_lane = track(
                &mut staged,
                mint,
                &original.track_id,
                "badgeBackground",
                false,
            );
            let badge_text_lane = if config.badge.text.trim().is_empty() {
                None
            } else {
                Some(track(&mut staged, mint, &original.track_id, "badge", false))
            };
            for mut piece in label(mint, &original, &badge_bg_lane, "badge", &config.badge) {
                if piece.kind == ClipKind::Text {
                    Arc::make_mut(&mut piece).track_id = badge_text_lane
                        .as_ref()
                        .expect("enabled badge text owns a text lane")
                        .clone();
                }
                layers.push(piece);
            }
        }
        let foreground = staged
            .active_mut()
            .clip_mut(&id)
            .ok_or(CommandError::MediaGone)?;
        // Migrate only unchanged legacy generated placement. Later manual
        // values remain the content's values, while the immutable restoration
        // snapshot remains available for Undo/removal compatibility.
        if !original.extra.contains_key(crate::canvas_viewport::KEY) {
            if let Some(previous)=original.extra.get("canvasPreset").cloned().and_then(|v|serde_json::from_value::<Config>(v).ok()) {
                let old_scale=(previous.foreground.width*w/fw).min(previous.foreground.height*h/fh);
                let old_x=previous.foreground.x+previous.foreground.width/2.0-0.5;
                let old_y=previous.foreground.y+previous.foreground.height/2.0-0.5;
                if (original.scale-old_scale).abs()<1e-9 {foreground.scale=base.scale;}
                if (original.offset_x-old_x).abs()<1e-9 {foreground.offset_x=base.offset_x;}
                if (original.offset_y-old_y).abs()<1e-9 {foreground.offset_y=base.offset_y;}
                if previous.preset>=4 && (original.rotation-previous.rotation).abs()<1e-9 {foreground.rotation=base.rotation;}
            }
        }
        foreground.background=Some(Background::None);
        // The owned Canvas Motion mask was already cleared above. The
        // fixed viewport defines the preset aperture; authored-mask conflicts
        // still use the existing explicit confirmation path.
        foreground.mask=None;
        let (x,y)=actual.center();
        let viewport=crate::canvas_viewport::Viewport {version:1,scale,x,y,width:actual.width,height:actual.height,
            rotation:config.rotation,radius:config.radius};
        if !viewport.valid() {return Err(error("Invalid Canvas aperture geometry"));}
        foreground.extra.insert(crate::canvas_viewport::KEY.into(),json!(viewport));
        foreground.extra.insert(
            "canvasPreset".into(),
            serde_json::to_value(&config).map_err(|e| error(e.to_string()))?,
        );
        foreground.extra.insert("canvasOriginal".into(), baseline);
        foreground.extra.insert(FOREGROUND_MASK.into(), json!(foreground.mask));
        for layer in &mut layers { stamp_companion(Arc::make_mut(layer)); }
        staged.active_mut().clips.extend(layers);
        // Empty legacy banner/headline lanes can be removed without touching
        // authored captions or clips that share those old tracks.
        let occupied: std::collections::HashSet<String> = staged
            .active()
            .clips
            .iter()
            .map(|c| c.track_id.clone())
            .collect();
        staged.active_mut().tracks.retain(|lane| {
            !(lane.extra.get("canvasTrack") == Some(&json!(&original.track_id))
                && matches!(
                    lane.extra.get("canvasRole").and_then(Value::as_str),
                    Some("banner" | "headline")
                )
                && !occupied.contains(&lane.id))
        });
        created = Some(id);
    }
    *project = staged;
    Ok(Outcome {
        created_id: created,
        applied: true,
    })
}


/// Catalogue retirement is an editor policy, not a change to legacy decoding.
pub const ACTIVE_PRESETS: [u32; 13] = [1, 2, 3, 4, 5, 6, 9, 12, 13, 14, 15, 16, 18];
/// Retired IDs 7 and 8 remain decodable. Unimplemented IDs are reserved.
const DECODABLE_PRESETS: [u32; 15] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 18];

/// Clear only known Canvas-owned layout data. A customized or protected
/// companion cannot be silently discarded. The old snapshot is used solely
/// for channels originally replaced by the layout, never as a full clip.
pub fn can_clear(project: &Project, clip: &Clip, locked: &[String]) -> bool {
    if !matches!(clip.kind, ClipKind::Image | ClipKind::Video)
        || clip.extra.contains_key("canvasOwner")
        || !crate::motion_preset::has_canvas_preset(clip)
        || locked.contains(&clip.track_id)
        || !project.active().tracks.iter().any(|t| t.id == clip.track_id && t.visible)
        || clip.extra.get("canvasOriginal").cloned()
            .and_then(|v| serde_json::from_value::<Clip>(v).ok()).is_none()
    { return false; }
    !project.active().clips.iter().any(|piece|
        piece.extra.get("canvasOwner") == Some(&json!(&clip.id))
            && (locked.contains(&piece.track_id)
                || !project.active().tracks.iter().any(|t| t.id == piece.track_id && t.visible)
                || (!untouched_companion(piece) && !unedited_legacy_headline(piece,clip))))
}

pub fn clear(project: &mut Project, targets: Vec<String>, locked: Vec<String>) -> Result<Outcome, CommandError> {
    let mut staged = project.clone();
    let mut changed = false;
    let mut affected_lanes = std::collections::HashSet::new();
    for id in targets {
        let Some(original) = staged.active().clip(&id).cloned() else { continue; };
        if !can_clear(&staged, &original, &locked) { continue; }
        let base: Clip = serde_json::from_value(original.extra["canvasOriginal"].clone())
            .map_err(|_| error("Canvas original snapshot is invalid"))?;
        // Legacy layouts wrote placement into ordinary transform channels.
        // Restore only unchanged generated values, and never keyed channels.
        let legacy = if !original.extra.contains_key(crate::canvas_viewport::KEY) {
            original.extra.get("canvasPreset").cloned()
                .and_then(|v| serde_json::from_value::<Config>(v).ok())
                .and_then(|cfg| {
                    let media = staged.media_by_id(&original.media_id)?;
                    let sw = f64::from(media.width?);
                    let sh = f64::from(media.height?);
                    let w = f64::from(staged.active().video.width);
                    let h = f64::from(staged.active().video.height);
                    if sw <= 0.0 || sh <= 0.0 || w <= 0.0 || h <= 0.0 {return None;}
                    let fit = (w/sw).min(h/sh);
                    Some(((cfg.foreground.width*w/(sw*fit)).min(cfg.foreground.height*h/(sh*fit)),
                        cfg.foreground.x+cfg.foreground.width/2.0-0.5,
                        cfg.foreground.y+cfg.foreground.height/2.0-0.5, cfg))
                })
        } else { None };
        let restore_mask = original.extra.get(FOREGROUND_MASK)
            .map_or_else(|| !authored_mask(&staged,&original), |saved| saved == &json!(original.mask));
        let foreground = staged.active_mut().clip_mut(&id).ok_or(CommandError::MediaGone)?;
        if foreground.background == Some(Background::None) {
            foreground.background = base.background.clone();
        }
        if restore_mask { foreground.mask = base.mask.clone(); }
        if let Some((scale,x,y,cfg)) = legacy {
            use crate::model::KeyProperty;
            if !foreground.is_keyed(KeyProperty::Scale) && (foreground.scale-scale).abs()<1e-9 {foreground.scale=base.scale;}
            if !foreground.is_keyed(KeyProperty::OffsetX) && (foreground.offset_x-x).abs()<1e-9 {foreground.offset_x=base.offset_x;}
            if !foreground.is_keyed(KeyProperty::OffsetY) && (foreground.offset_y-y).abs()<1e-9 {foreground.offset_y=base.offset_y;}
            if cfg.preset>=4 && !foreground.is_keyed(KeyProperty::Rotation) && (foreground.rotation-cfg.rotation).abs()<1e-9 {foreground.rotation=base.rotation;}
        }
        for key in ["canvasPreset", crate::canvas_viewport::KEY, "canvasOriginal", FOREGROUND_MASK] {
            foreground.extra.remove(key);
        }
        staged.active_mut().clips.retain(|piece| {
            if piece.extra.get("canvasOwner") == Some(&json!(&id)) {
                affected_lanes.insert(piece.track_id.clone());
                false
            } else {true}
        });
        changed=true;
    }
    let occupied: std::collections::HashSet<_> = staged.active().clips.iter().map(|c| c.track_id.clone()).collect();
    staged.active_mut().tracks.retain(|t| !affected_lanes.contains(&t.id)
        || occupied.contains(&t.id) || locked.contains(&t.id) || !t.extra.contains_key("canvasTrack"));
    if changed { *project=staged; }
    Ok(Outcome {created_id:None, applied:changed})
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{MediaItem, MediaKind};
    fn assert_no_canvas_headline(project: &Project) {
        assert!(project.active().clips.iter().all(|clip| {
            !matches!(
                clip.extra.get("canvasRole").and_then(Value::as_str),
                Some("headline" | "headlineText")
            )
        }));
        assert!(project.active().tracks.iter().all(|lane| {
            !matches!(
                lane.extra.get("canvasRole").and_then(Value::as_str),
                Some("headline" | "banner")
            )
        }));
    }
    fn add_real_caption(project: &mut Project) {
        let mut caption = Clip::blank(
            "real-caption",
            "T1",
            ClipKind::Text,
            "Real user caption",
            2.5,
            1.5,
        );
        caption.text = Some(TextStyle {
            content: "Author's own caption".into(),
            ..TextStyle::default()
        });
        project.active_mut().clips.push(Arc::new(caption));
    }

    fn legacy_bar(project: &mut Project, mint: &mut IdMint, cfg: &Config) -> String {
        project
            .active_mut()
            .clip_mut("c1")
            .unwrap()
            .extra
            .insert("canvasPreset".into(), serde_json::to_value(cfg).unwrap());
        let lane = track(project, mint, "T1", "banner", false);
        let bar = rectangle(
            mint,
            project.active().clip("c1").unwrap(),
            &lane,
            "headline",
            cfg.headline.rect,
            &cfg.headline.background,
            cfg.headline.radius,
            cfg.headline.opacity,
        );
        let id = bar.id.clone();
        project.active_mut().clips.push(bar);
        id
    }
    fn fixture() -> (Project, IdMint) {
        let mut p = Project::new();
        p.media.push(MediaItem {
            id: "m1".into(),
            kind: MediaKind::Video,
            width: Some(1080),
            height: Some(1920),
            duration: Some(10.0),
            ..Default::default()
        });
        let mut c = Clip::blank("c1", "T1", ClipKind::Video, "source", 2.0, 4.0);
        c.media_id = "m1".into();
        c.source_start = 1.0;
        p.active_mut().clips.push(Arc::new(c));
        let mut m = IdMint::default();
        m.adopt_project(&p);
        (p, m)
    }
    fn generated_animation(clip: &mut Clip, canvas: bool) {
        use crate::motion_preset::{MotionPreset, replace_generated};
        use crate::canvas_motion::{CanvasMotionPreset, CanvasMotionControls, rounded_mask};
        let expected = clip.keys.clone();
        let (id, keys, style) = if canvas {
            let p = CanvasMotionPreset::ALL[0];
            (p.id(), p.keys(clip, 1.0/30.0, CanvasMotionControls::default()).unwrap(),
                Some((0.5, rounded_mask(1080, 1920, 0.1))))
        } else {
            let p = MotionPreset::PushIn;
            (p.id(), p.keys(clip, 1.0/30.0).unwrap().0.to_vec(), None)
        };
        replace_generated(clip, &expected, keys, id.into(), style).unwrap();
    }

    #[test]
    fn canvas_preset_replaces_canvas_animation_and_blocks_later_canvas_motion() {
        use crate::motion_preset::{generated_ownership, replace_generated, has_canvas_preset};
        use crate::canvas_motion::{CanvasMotionPreset, CanvasMotionControls};
        let (mut project, mut mint) = fixture();
        generated_animation(project.active_mut().clip_mut("c1").unwrap(), true);
        apply(&mut project,&mut mint,Config::preset(6).unwrap(),vec!["c1".into()],vec![]).unwrap();
        let clip=project.active_mut().clip_mut("c1").unwrap();
        assert!(has_canvas_preset(clip));
        assert!(clip.keys.is_empty());
        assert!(generated_ownership(clip).is_none());
        assert!(clip.mask.is_none());
        assert_eq!(clip.background,Some(Background::None));
        let baseline:Clip=serde_json::from_value(clip.extra["canvasOriginal"].clone()).unwrap();
        assert!(baseline.keys.is_empty());
        assert!(generated_ownership(&baseline).is_none());
        let before=clip.clone();
        let p=CanvasMotionPreset::ALL[0];
        let keys=p.keys(clip,1.0/30.0,CanvasMotionControls::default()).unwrap();
        assert!(replace_generated(clip,&[],keys,p.id().into(),Some((0.5,None))).is_err());
        assert_eq!(*clip,before);
    }

    #[test]
    fn canvas_preset_and_basic_animation_coexist_in_both_orders() {
        use crate::motion_preset::{generated_ownership, clear_generated};
        for animation_first in [true,false] {
            let (mut project,mut mint)=fixture();
            if animation_first {generated_animation(project.active_mut().clip_mut("c1").unwrap(),false);}
            apply(&mut project,&mut mint,Config::preset(6).unwrap(),vec!["c1".into()],vec![]).unwrap();
            if !animation_first {generated_animation(project.active_mut().clip_mut("c1").unwrap(),false);}
            let clip=project.active().clip("c1").unwrap();
            let keys=clip.keys.clone();
            assert!(!keys.is_empty());
            assert_eq!(generated_ownership(clip).unwrap().preset,"motion.push-in");
            apply(&mut project,&mut mint,Config::preset(5).unwrap(),vec!["c1".into()],vec![]).unwrap();
            let clip=project.active_mut().clip_mut("c1").unwrap();
            assert_eq!(clip.keys,keys);
            assert_eq!(generated_ownership(clip).unwrap().preset,"motion.push-in");
            let viewport=crate::canvas_viewport::Viewport::of(clip);
            assert!(viewport.is_some());
            clear_generated(clip,&keys).unwrap();
            assert_eq!(crate::canvas_viewport::Viewport::of(clip),viewport);
            assert!(clip.extra.contains_key("canvasPreset"));
        }
    }

    #[test]
    fn canvas_preset_and_physics_animation_coexist_in_both_orders() {
        use crate::motion_preset::{generated_ownership, replace_generated};
        use crate::physics_motion::PhysicsPreset;
        for physics_first in [true,false] {
            let (mut project,mut mint)=fixture();
            if !physics_first {
                apply(&mut project,&mut mint,Config::preset(6).unwrap(),vec!["c1".into()],vec![]).unwrap();
            }
            let clip=project.active_mut().clip_mut("c1").unwrap();
            let p=PhysicsPreset::SpringZoom;
            let keys=p.keys(clip,1.0/30.0).unwrap().0;
            replace_generated(clip,&[],keys,p.id().into(),None).unwrap();
            let keys=clip.keys.clone();
            if physics_first {
                apply(&mut project,&mut mint,Config::preset(6).unwrap(),vec!["c1".into()],vec![]).unwrap();
            }
            let clip=project.active().clip("c1").unwrap();
            assert_eq!(clip.keys,keys);
            assert_eq!(generated_ownership(clip).unwrap().preset,p.id());
            assert!(crate::canvas_viewport::Viewport::of(clip).is_some());
        }
    }

    #[test]
    fn canvas_preset_does_not_silently_delete_manually_edited_canvas_animation() {
        let (mut project,mut mint)=fixture();
        let clip=project.active_mut().clip_mut("c1").unwrap();
        generated_animation(clip,true);
        clip.keys[0].value += 0.001;
        let before=project.clone();
        assert!(apply_with_confirmation(&mut project,&mut mint,Config::preset(6).unwrap(),
            vec!["c1".into()],vec![],true).is_err());
        assert_eq!(project.active().clips,before.active().clips);
    }

    #[test]
    fn clear_canvas_preserves_current_content_and_animation() {
        let (mut project, mut mint)=fixture();
        add_real_caption(&mut project);
        let base=project.active().clip("c1").unwrap().clone();
        apply(&mut project,&mut mint,Config::preset(6).unwrap(),vec!["c1".into()],vec![]).unwrap();
        {
            let clip=project.active_mut().clip_mut("c1").unwrap();
            clip.scale=1.2;
            generated_animation(clip,false);
        }
        let current=project.active().clip("c1").unwrap().clone();
        assert!(clear(&mut project,vec!["c1".into()],vec![]).unwrap().applied);
        let clip=project.active().clip("c1").unwrap();
        assert!(!crate::motion_preset::has_canvas_preset(clip));
        assert_eq!(clip.keys,current.keys);
        assert_eq!(clip.scale,current.scale);
        assert_eq!(clip.background,base.background);
        assert_eq!(clip.mask,base.mask);
        assert_eq!(crate::motion_preset::generated_ownership(clip),
            crate::motion_preset::generated_ownership(&current));
        assert_eq!(clip.media_id,current.media_id);
        assert_eq!(clip.source_start,current.source_start);
        assert!(project.active().clip("real-caption").is_some());
        assert!(project.active().clips.iter().all(|c| !c.extra.contains_key("canvasOwner")));
        assert!(!clear(&mut project,vec!["c1".into()],vec![]).unwrap().applied);
    }

    #[test]
    fn clear_canvas_skips_locked_and_customized_companions() {
        let (mut project, mut mint)=fixture();
        // Retired presets remain readable and clearable in existing projects.
        apply(&mut project,&mut mint,Config::preset(7).unwrap(),vec!["c1".into()],vec![]).unwrap();
        let piece=project.active().clips.iter().find(|c|c.extra.contains_key("canvasOwner")).unwrap().clone();
        assert!(!clear(&mut project,vec!["c1".into()],vec![piece.track_id.clone()]).unwrap().applied);
        project.active_mut().clip_mut(&piece.id).unwrap().name="Customized".into();
        assert!(!clear(&mut project,vec!["c1".into()],vec![]).unwrap().applied);
        assert!(crate::motion_preset::has_canvas_preset(project.active().clip("c1").unwrap()));
        assert_eq!(project.active().clip(&piece.id).unwrap().name,"Customized");
    }

    #[test]
    fn independent_presets_validate_and_round_trip() {
        for n in DECODABLE_PRESETS {
            let p = Config::preset(n).unwrap();
            p.validate().unwrap();
            assert_eq!(
                p,
                Config::from_json(&serde_json::to_string(&p).unwrap()).unwrap()
            );
            assert!(p.headline.text.is_empty());
            assert!(!p.headline.enabled);
            assert!(!p.badge.enabled);
        }
    }
    #[test]
    fn planned_families_are_real_compositions_not_new_color_variants() {
        let blur = Config::preset(3).unwrap();
        let gallery = Config::preset(4).unwrap();
        let floating = Config::preset(5).unwrap();
        let print = Config::preset(6).unwrap();
        let cinema = Config::preset(7).unwrap();
        let editorial = Config::preset(8).unwrap();
        assert_eq!(blur.background, "blur");
        assert_eq!(gallery.foreground.x, 0.09);
        assert!(floating.shadow_opacity > 0.0);
        assert!(print.mat_bottom > 0.0 && print.rotation != 0.0);
        assert!(cinema.foreground.height < 0.75);
        assert!(editorial.foreground.x > 0.3);
        assert!(Config::preset(10).is_none());
        assert!(Config::preset(21).is_none());
        // Old JSON omits the new optional fields, and must still be loadable.
        let mut old = serde_json::to_value(Config::preset(1).unwrap()).unwrap();
        for key in ["shadowOpacity", "shadowOffset", "shadowColor", "matBottom", "matColor", "rotation"] {
            old.as_object_mut().unwrap().remove(key);
        }
        Config::from_json(&old.to_string()).unwrap();
    }
    #[test]
    fn native_layers_preserve_media_timing_and_reapplication_replaces_owned_layers() {
        let (mut p, mut m) = fixture();
        let mut c = Config::preset(2).unwrap();
        // Even a legacy profile explicitly enabling a headline cannot
        // make a canvas-generated title/bar. Badge is still opt-in.
        c.headline.enabled = true;
        c.headline.text = "editable headline".into();
        c.badge.enabled = true;
        apply(&mut p, &mut m, c.clone(), vec!["c1".into()], vec![]).unwrap();
        let count = p.active().clips.len();
        let source = p.active().clip("c1").unwrap();
        assert_eq!(
            (source.start, source.duration, source.source_start),
            (2.0, 4.0, 1.0)
        );
        assert_eq!(source.media_id, "m1");
        assert_eq!(source.scale,1.0);
        assert!(crate::canvas_viewport::Viewport::of(source).unwrap().scale<1.0);
        assert_no_canvas_headline(&p);
        assert!(p.active().clips.iter().any(|c|
            c.extra.get("canvasRole") == Some(&json!("badgeText"))));
        apply(&mut p, &mut m, c, vec!["c1".into()], vec![]).unwrap();
        assert_eq!(count, p.active().clips.len());
        assert_eq!(p.media.len(), 1);
    }
    #[test]
    fn selected_and_all_canvas_apply_preserve_real_captions_and_other_source() {
        for preset in 1..=8 {
            let (mut project, mut mint) = fixture();
            let mut second = Clip::blank("c2", "T1", ClipKind::Video, "second", 7.0, 2.0);
            second.media_id = "m1".into();
            project.active_mut().clips.push(Arc::new(second));
            add_real_caption(&mut project);
            mint.adopt_project(&project);
            let original_caption =
                serde_json::to_value(project.active().clip("real-caption").unwrap()).unwrap();
            let original_second =
                serde_json::to_value(project.active().clip("c2").unwrap()).unwrap();

            let cfg = Config::preset(preset).unwrap();
            apply(&mut project, &mut mint, cfg.clone(), vec!["c1".into()], vec![]).unwrap();
            assert_no_canvas_headline(&project);
            assert_eq!(
                serde_json::to_value(project.active().clip("c2").unwrap()).unwrap(),
                original_second
            );
            assert_eq!(
                serde_json::to_value(project.active().clip("real-caption").unwrap()).unwrap(),
                original_caption
            );
            apply(
                &mut project,
                &mut mint,
                cfg,
                vec!["c1".into(), "c2".into()],
                vec![],
            )
            .unwrap();
            assert_no_canvas_headline(&project);
            for id in ["c1", "c2"] {
                let foreground = project.active().clip(id).unwrap();
                assert!(foreground.extra.get("canvasPreset").is_some());
                assert_eq!(foreground.background, Some(Background::None));
            }
            assert_eq!(
                serde_json::to_value(project.active().clip("real-caption").unwrap()).unwrap(),
                original_caption
            );
            assert!(project.active().clips.iter().all(|c|
                !c.name.to_lowercase().contains("canvas headline")));
        }
    }

    #[test]
    fn reapply_removes_only_untouched_legacy_bars_and_empty_tracks() {
        let (mut project, mut mint) = fixture();
        add_real_caption(&mut project);
        apply(
            &mut project,
            &mut mint,
            Config::preset(1).unwrap(),
            vec!["c1".into()],
            vec![],
        )
        .unwrap();
        let mut old = Config::preset(1).unwrap();
        old.headline.enabled = true;
        let legacy_id = legacy_bar(&mut project, &mut mint, &old);
        let caption_before =
            serde_json::to_value(project.active().clip("real-caption").unwrap()).unwrap();
        apply(
            &mut project,
            &mut mint,
            Config::preset(2).unwrap(),
            vec!["c1".into()],
            vec![],
        )
        .unwrap();
        assert_no_canvas_headline(&project);
        assert!(project.active().clip(&legacy_id).is_none());
        assert_eq!(
            serde_json::to_value(project.active().clip("real-caption").unwrap()).unwrap(),
            caption_before
        );
    }

    #[test]
    fn customized_legacy_profile_is_not_silently_removed() {
        let (mut project, mut mint) = fixture();
        apply(
            &mut project,
            &mut mint,
            Config::preset(1).unwrap(),
            vec!["c1".into()],
            vec![],
        )
        .unwrap();
        let mut authored = Config::preset(1).unwrap();
        authored.headline.enabled = true;
        authored.headline.background = "#cc0088".into();
        let legacy_id = legacy_bar(&mut project, &mut mint, &authored);
        let before = serde_json::to_value(&project).unwrap();
        assert!(apply(
            &mut project,
            &mut mint,
            Config::preset(2).unwrap(),
            vec!["c1".into()],
            vec![],
        )
        .is_err());
        assert!(project.active().clip(&legacy_id).is_some());
        assert_eq!(serde_json::to_value(&project).unwrap(), before);
    }

    #[test]
    fn reapply_refuses_customized_legacy_headlines_without_changing_project() {
        let (mut project, mut mint) = fixture();
        apply(
            &mut project,
            &mut mint,
            Config::preset(1).unwrap(),
            vec!["c1".into()],
            vec![],
        )
        .unwrap();
        let mut old = Config::preset(1).unwrap();
        old.headline.enabled = true;
        let id = legacy_bar(&mut project, &mut mint, &old);
        project.active_mut().clip_mut(&id).unwrap().opacity = 0.42;
        let previous = serde_json::to_value(&project).unwrap();
        let error = apply(
            &mut project,
            &mut mint,
            Config::preset(3).unwrap(),
            vec!["c1".into()],
            vec![],
        );
        assert!(error.is_err());
        assert_eq!(serde_json::to_value(&project).unwrap(), previous);

        // Previously authored headline text must also be handled explicitly.
        project.active_mut().clip_mut(&id).unwrap().opacity = 1.0;
        let caption_lane = track(&mut project, &mut mint, "T1", "headline", false);
        let mut title = Clip::blank(
            mint.next("c"),
            &caption_lane,
            ClipKind::Text,
            "Canvas headline text",
            2.0,
            4.0,
        );
        title.text = Some(TextStyle {
            content: "My edited lower-third".into(),
            ..TextStyle::default()
        });
        project.active_mut().clips.push(tagged(title, "c1", "headlineText"));
        let before_text = serde_json::to_value(&project).unwrap();
        assert!(apply(
            &mut project,
            &mut mint,
            Config::preset(2).unwrap(),
            vec!["c1".into()],
            vec![],
        )
        .is_err());
        assert_eq!(serde_json::to_value(&project).unwrap(), before_text);
    }

    #[test]
    fn canvas_apply_undo_redo_and_save_reopen_stay_caption_free() {
        let (mut project, _) = fixture();
        project.media[0].path = "source.mp4".into();
        add_real_caption(&mut project);
        let settings = crate::doc::DocumentSettings {
            name: "Canvas undo and GPU fixture".into(),
            width: 1920,
            height: 1080,
            rate_num: 30,
            rate_den: 1,
        };
        let document = crate::doc::to_document(&settings, &project);
        for preset in 1..=8 {
            let mut editor = crate::editor::Editor::from_document(&document).unwrap();
            let before = serde_json::to_value(editor.project()).unwrap();
            editor
                .apply(crate::commands::Command::ApplyCanvasPreset {
                    config: Config::preset(preset).unwrap(),
                    targets: vec!["c1".into()],
                    locked_tracks: vec![],
                })
                .unwrap();
            let after = serde_json::to_value(editor.project()).unwrap();
            assert_ne!(before, after);
            assert_no_canvas_headline(editor.project());
            assert!(editor.undo());
            assert_eq!(serde_json::to_value(editor.project()).unwrap(), before);
            assert!(editor.redo());
            assert_eq!(serde_json::to_value(editor.project()).unwrap(), after);
            let gpu_doc = editor.to_document(&settings);
            let reopened = crate::doc::from_document(&gpu_doc).unwrap();
            assert_no_canvas_headline(&reopened);
            assert_eq!(
                reopened.active().clip("real-caption").unwrap().text.as_ref().unwrap().content,
                "Author's own caption"
            );
            // The GPU ZIP consumes the same serialized track/clip data.
            assert_eq!(reopened.active().clip("c1").unwrap().media_id, "m1");
            assert!(reopened.active().clips.iter().any(|clip|
                clip.extra.get("canvasRole") == Some(&json!("backdrop"))));
        }
    }

    #[test]
    fn invalid_or_locked_targets_leave_project_untouched() {
        let (mut p, mut m) = fixture();
        let before = p.clone();
        assert!(
            apply(
                &mut p,
                &mut m,
                Config::preset(1).unwrap(),
                vec!["c1".into()],
                vec!["T1".into()]
            )
            .is_err()
        );
        assert_eq!(
            serde_json::to_value(before).unwrap(),
            serde_json::to_value(p).unwrap()
        );
    }
    #[test]
    fn portrait_background_is_owned_and_does_not_stretch_foreground() {
        let (mut p, mut m) = fixture();
        apply(
            &mut p,
            &mut m,
            Config::preset(3).unwrap(),
            vec!["c1".into()],
            vec![],
        )
        .unwrap();
        let fg = p.active().clip("c1").unwrap();
        assert_eq!((fg.stretch_x, fg.stretch_y), (1.0, 1.0));
        let bg = p
            .active()
            .clips
            .iter()
            .find(|c| c.extra.get("canvasRole") == Some(&json!("backdrop")))
            .unwrap();
        assert_eq!(
            p.active().background_of(bg),
            Some(&Background::Blur { amount: 0.75 })
        );
    }
    #[test]
    fn mixed_canvas_presets_do_not_steal_the_main_background_lane() {
        let (mut project, mut mint) = fixture();
        let mut ordinary = Clip::blank("c2", "T1", ClipKind::Video, "ordinary", 6.0, 4.0);
        ordinary.media_id = "m1".into();
        ordinary.background = Some(Background::Blur { amount: 0.4 });
        project.active_mut().clips.push(Arc::new(ordinary));
        apply(
            &mut project,
            &mut mint,
            Config::preset(3).unwrap(),
            vec!["c1".into()],
            vec![],
        )
        .unwrap();
        let timeline = project.active();
        let backdrop = timeline
            .clips
            .iter()
            .find(|c| c.extra.get("canvasRole") == Some(&json!("backdrop")))
            .unwrap();
        assert_ne!(backdrop.track_id, "T1");
        assert_eq!(timeline.background_lane(), Some("T1"));
        assert_eq!(
            timeline.background_of(timeline.clip("c2").unwrap()),
            Some(&Background::Blur { amount: 0.4 })
        );
        assert_eq!(
            timeline.background_of(backdrop),
            Some(&Background::Blur { amount: 0.75 })
        );
        let settings = crate::doc::DocumentSettings {
            name: "Mixed canvas".into(),
            width: 1920,
            height: 1080,
            rate_num: 30,
            rate_den: 1,
        };
        let reopened =
            crate::doc::from_document(&crate::doc::to_document(&settings, &project)).unwrap();
        assert_eq!(reopened.active().background_lane(), Some("T1"));
        assert_eq!(
            reopened
                .active()
                .background_of(reopened.active().clip("c2").unwrap()),
            Some(&Background::Blur { amount: 0.4 })
        );
    }

    #[test]
    fn generated_canvas_documents_reopen_with_editable_layers() {
        for number in DECODABLE_PRESETS {
            for portrait in [true, false] {
                let (mut p, mut mint) = fixture();
                p.media[0].path = "source.mp4".into();
                if !portrait {
                    p.media[0].width = Some(1920);
                    p.media[0].height = Some(1080);
                }
                let mut config = Config::preset(number).unwrap();
                config.headline.enabled = true;
                config.headline.text = "Editable headline".into();
                config.badge.enabled = true;
                apply(&mut p, &mut mint, config, vec!["c1".into()], vec![]).unwrap();
                let settings = crate::doc::DocumentSettings {
                    name: "Canvas fixture".into(),
                    width: 1920,
                    height: 1080,
                    rate_num: 30,
                    rate_den: 1,
                };
                let doc = crate::doc::to_document(&settings, &p);
                let reopened = crate::doc::from_document(&doc).unwrap();
                assert_eq!(reopened.active().clips.len(), p.active().clips.len());
                assert_eq!(reopened.active().clip("c1").unwrap().media_id, "m1");
                assert_no_canvas_headline(&reopened);
                if let Ok(folder) = std::env::var("CONCAT_CANVAS_FIXTURE_DIR") {
                    std::fs::create_dir_all(&folder).unwrap();
                    std::fs::write(
                        std::path::Path::new(&folder)
                            .join(format!("canvas-{number}-{portrait}.json")),
                        serde_json::to_vec_pretty(&doc).unwrap(),
                    )
                    .unwrap();
                }
            }
        }
    }
    #[test]
    fn reapply_background_uses_current_playback_state() {
        let (mut p, mut m) = fixture();
        let config = Config::preset(3).unwrap();
        apply(&mut p,&mut m,config.clone(),vec!["c1".into()],vec![]).unwrap();
        let foreground = p.active_mut().clip_mut("c1").unwrap();
        foreground.start = 7.0;
        foreground.duration = 1.5;
        foreground.source_start = 3.0;
        foreground.speed = 2.0;
        apply(&mut p,&mut m,config,vec!["c1".into()],vec![]).unwrap();
        let bg = p.active().clips.iter().find(|c|c.extra.get("canvasRole") == Some(&json!("backdrop"))).unwrap();
        assert_eq!((bg.start,bg.duration,bg.source_start,bg.speed),(7.0,1.5,3.0,2.0));
        let foreground = p.active().clip("c1").unwrap();
        let baseline: Clip = serde_json::from_value(foreground.extra["canvasOriginal"].clone()).unwrap();
        assert_eq!((baseline.start,baseline.duration,baseline.source_start),(2.0,4.0,1.0));
    }

    #[test]
    fn locked_companion_reapply_is_atomic() {
        let (mut p, mut m) = fixture();
        let config = Config::preset(3).unwrap();
        apply(&mut p,&mut m,config.clone(),vec!["c1".into()],vec![]).unwrap();
        let lane = p.active().clips.iter().find(|c|c.extra.contains_key("canvasOwner")).unwrap().track_id.clone();
        let before = p.clone();
        let result = apply(&mut p,&mut m,config,vec!["c1".into()],vec![lane]);
        assert!(result.is_err());
        assert_eq!(p,before);
    }
    fn linked_editor() -> crate::Editor {
        let (mut project,mut mint) = fixture();
        project.media[0].path="source.mp4".into();
        apply(&mut project,&mut mint,Config::preset(3).unwrap(),vec!["c1".into()],vec![]).unwrap();
        let settings=crate::DocumentSettings {name:"Canvas links".into(),width:1920,height:1080,rate_num:30,rate_den:1};
        crate::Editor::from_document(&crate::to_document(&settings,&project)).unwrap()
    }
    fn group_size(project:&Project,owner:&str)->usize {
        project.active().clips.iter().filter(|c|c.extra.get("canvasOwner")==Some(&json!(owner))).count()
    }
    fn assert_group_timing(project:&Project,owner:&str) {
        let foreground=project.active().clip(owner).unwrap();
        for piece in &project.active().clips {
            if piece.extra.get("canvasOwner")!=Some(&json!(owner)) {continue;}
            assert_eq!((piece.start,piece.duration),(foreground.start,foreground.duration));
            if piece.extra.get("canvasRole")==Some(&json!("backdrop")) {
                assert_eq!((piece.source_start,piece.speed),(foreground.source_start,foreground.speed));
            }
        }
    }
    #[test]
    fn deleting_canvas_owner_removes_companions_in_one_undo_step() {
        for ripple in [false,true] {
            let mut editor=linked_editor();
            let before=editor.project().clone();
            assert!(group_size(&before,"c1")>0);
            editor.apply(crate::Command::RemoveClips {clip_ids:vec!["c1".into()],ripple}).unwrap();
            assert_eq!(group_size(editor.project(),"c1"),0);
            assert!(editor.project().active().clip("c1").is_none());
            assert!(editor.undo()); assert_eq!(editor.project(),&before);
            assert!(editor.redo()); assert_eq!(group_size(editor.project(),"c1"),0);
        }
    }
    #[test]
    fn canvas_trim_move_speed_and_split_keep_independent_groups() {
        use crate::commands::{Command,TrimEdge,ClipMove};
        let mut editor=linked_editor();
        for command in [
            Command::TrimClip {clip_id:"c1".into(),edge:TrimEdge::Start,delta:0.5,ripple:false},
            Command::TrimClip {clip_id:"c1".into(),edge:TrimEdge::End,delta:-0.5,ripple:false},
            Command::MoveClips {moves:vec![ClipMove {clip_id:"c1".into(),track_id:"T1".into(),start:4.0}]},
            Command::SetClipSpeed {clip_id:"c1".into(),speed:1.5},
        ] {editor.apply(command).unwrap();assert_group_timing(editor.project(),"c1");}
        let count=group_size(editor.project(),"c1");
        let tail=editor.apply(Command::SplitClips {clip_ids:vec!["c1".into()],time:5.0}).unwrap().created_id.unwrap();
        assert_eq!(group_size(editor.project(),"c1"),count);
        assert_eq!(group_size(editor.project(),&tail),count);
        assert_group_timing(editor.project(),"c1");assert_group_timing(editor.project(),&tail);
        assert!(conflicts(editor.project(),&["c1".into(),tail.clone()]).is_empty());
        editor.apply(Command::RemoveClips {clip_ids:vec!["c1".into()],ripple:false}).unwrap();
        assert_eq!(group_size(editor.project(),&tail),count);
        assert_group_timing(editor.project(),&tail);
    }
    #[test]
    fn selected_owner_and_companions_split_only_once_and_rebind_tail() {
        let mut editor=linked_editor();
        let ids=editor.project().active().clips.iter().map(|c|c.id.clone()).collect();
        let count=editor.project().active().clips.len();
        editor.apply(crate::Command::SplitClips {clip_ids:ids,time:4.0}).unwrap();
        assert_eq!(editor.project().active().clips.len(),count*2);
        let owners:Vec<_>=editor.project().active().clips.iter().filter(|c|!c.extra.contains_key("canvasOwner")).map(|c|c.id.clone()).collect();
        assert_eq!(owners.len(),2);
        for id in owners {assert_group_timing(editor.project(),&id);assert!(group_size(editor.project(),&id)>0);}
    }
    #[test]
    fn indirect_canvas_edits_on_locked_companions_roll_back() {
        use crate::commands::{Command,TrimEdge};
        let mut editor=linked_editor();
        let before=editor.project().clone();
        let locked=before.active().clips.iter().find(|c|c.extra.contains_key("canvasOwner")).unwrap().track_id.clone();
        for command in [Command::RemoveClips {clip_ids:vec!["c1".into()],ripple:true},
            Command::SplitClips {clip_ids:vec!["c1".into()],time:4.0},
            Command::TrimClip {clip_id:"c1".into(),edge:TrimEdge::End,delta:-1.0,ripple:false}] {
            assert!(editor.apply(Command::ProtectCanvasTracks {timeline_id:before.active().id.clone(),locked_tracks:vec![locked.clone()],command:Box::new(command)}).is_err());
            assert_eq!(editor.project(),&before);
        }
    }
    #[test]
    fn current_authored_masks_and_customized_layers_require_explicit_replacement() {
        use crate::commands::{Command,ClipPatch};
        let mut editor=linked_editor();
        let companion=editor.project().active().clips.iter().find(|c|c.extra.contains_key("canvasOwner")).unwrap().id.clone();
        editor.apply(Command::UpdateClip {clip_id:"c1".into(),patch:ClipPatch {mask:Some(Some(ClipMask::of(MaskShape::Circle))),..Default::default()}}).unwrap();
        editor.apply(Command::UpdateClip {clip_id:companion,patch:ClipPatch {name:Some("My custom background".into()),..Default::default()}}).unwrap();
        let found=conflicts(editor.project(),&["c1".into()]);
        assert_eq!(found.masks.len(),1);assert_eq!(found.customized_layers,vec!["My custom background"]);
        let before=editor.project().clone();
        let config=Config::preset(6).unwrap();
        assert!(editor.apply(Command::ApplyCanvasPreset {config:config.clone(),targets:vec!["c1".into()],locked_tracks:vec![]}).is_err());
        assert_eq!(editor.project(),&before); // Cancel or refused Apply cannot change data.
        editor.apply(Command::ApplyCanvasPresetConfirmed {config,targets:vec!["c1".into()],locked_tracks:vec![]}).unwrap();
        assert!(conflicts(editor.project(),&["c1".into()]).is_empty());
        assert!(editor.undo());assert_eq!(editor.project(),&before);
    }
    #[test]
    fn first_application_authored_mask_confirmation_does_not_block_future_reapply() {
        let (mut project,mut mint)=fixture();
        project.active_mut().clip_mut("c1").unwrap().mask=Some(ClipMask::of(MaskShape::Circle));
        let before=project.clone();let config=Config::preset(3).unwrap();
        assert!(apply(&mut project,&mut mint,config.clone(),vec!["c1".into()],vec![]).is_err());
        assert_eq!(project,before);
        apply_with_confirmation(&mut project,&mut mint,config.clone(),vec!["c1".into()],vec![],true).unwrap();
        apply(&mut project,&mut mint,config,vec!["c1".into()],vec![]).unwrap();
        assert!(conflicts(&project,&["c1".into()]).is_empty());
    }

    #[test]
    fn fixed_viewport_preserves_content_adjustments_in_both_application_orders() {
        use crate::commands::{apply as command,Command};
        let (mut before,mut bm)=fixture();let (mut after,mut am)=fixture();
        let adjustment=Command::SetClipTransform {clip_id:"c1".into(),scale:Some(2.2),offset_x:Some(0.15),offset_y:Some(-0.1),rotation:Some(21.0),stretch_x:None,stretch_y:None};
        command(&mut before,&mut bm,adjustment.clone()).unwrap();
        apply(&mut before,&mut bm,Config::preset(6).unwrap(),vec!["c1".into()],vec![]).unwrap();
        apply(&mut after,&mut am,Config::preset(6).unwrap(),vec!["c1".into()],vec![]).unwrap();
        command(&mut after,&mut am,adjustment).unwrap();
        let a=before.active().clip("c1").unwrap();let b=after.active().clip("c1").unwrap();
        assert_eq!((a.scale,a.offset_x,a.offset_y,a.rotation),(b.scale,b.offset_x,b.offset_y,b.rotation));
        assert_eq!(crate::canvas_viewport::Viewport::of(a),crate::canvas_viewport::Viewport::of(b));
        apply(&mut after,&mut am,Config::preset(5).unwrap(),vec!["c1".into()],vec![]).unwrap();
        let b=after.active().clip("c1").unwrap();
        assert_eq!((b.scale,b.offset_x,b.offset_y,b.rotation),(2.2,0.15,-0.1,21.0));
    }
    #[test]
    fn content_keyframes_compose_inside_fixed_aperture_and_survive_reapply() {
        use crate::model::{KeyProperty,KeyEase};
        let (mut project,mut mint)=fixture();
        project.media[0].path="source.mp4".into();
        let clip=project.active_mut().clip_mut("c1").unwrap();
        clip.set_key(KeyProperty::Scale,0.0,1.0,KeyEase::LINEAR);
        clip.set_key(KeyProperty::Scale,0.75,2.5,KeyEase::LINEAR);
        clip.offset_x=0.12;clip.offset_y=-0.08;clip.rotation=22.0;
        let keys=clip.keys.clone();
        apply(&mut project,&mut mint,Config::preset(6).unwrap(),vec!["c1".into()],vec![]).unwrap();
        apply(&mut project,&mut mint,Config::preset(6).unwrap(),vec!["c1".into()],vec![]).unwrap();
        let clip=project.active().clip("c1").unwrap();assert_eq!(clip.keys,keys);
        let frame=crate::canvas_viewport::Viewport::of(clip).unwrap().engine(1920.0/1080.0);
        let aperture=frame.aperture();
        let samples:Vec<_>=[0,15,30,60,90].into_iter().map(|index|{
            let at=f64::from(index)/30.0/clip.duration;
            let t=frame.compose(concat_core::timeline::Transform {scale:clip.value_at(KeyProperty::Scale,at),offset_x:clip.offset_x,offset_y:clip.offset_y,rotation:clip.rotation,..Default::default()});
            serde_json::json!({"frame":index+60,"scale":t.scale,"pan_x":t.offset_x,"pan_y":t.offset_y,"rotation_degrees":t.rotation})
        }).collect();
        let mask=aperture.uniform(1920.0,1080.0,1.0/1080.0);
        let coverage:Vec<_>=(0..=10).flat_map(|x|(0..=10).map(move|y|{
            let (u,v)=(x as f32/10.0,y as f32/10.0);
            serde_json::json!({"u":u,"v":v,"coverage":concat_core::mask::coverage(&mask,u,v)})
        })).collect();
        if let Ok(directory)=std::env::var("CONCAT_CANVAS_FIXTURE_DIR") {
            std::fs::create_dir_all(&directory).unwrap();
            let settings=crate::DocumentSettings {name:"Viewport".into(),width:1920,height:1080,rate_num:30,rate_den:1};
            let data=serde_json::json!({"document":crate::to_document(&settings,&project),"samples":samples,"coverage":coverage});
            std::fs::write(std::path::Path::new(&directory).join("viewport-contract.json"),serde_json::to_vec_pretty(&data).unwrap()).unwrap();
        }
    }

    #[test]
    fn picture_size_is_relative_to_each_preset_and_preserves_center_and_aspect() {
        for id in DECODABLE_PRESETS {
            let mut config = Config::preset(id).unwrap();
            let before = config.foreground;
            assert!((config.picture_size_percent() - 100.0).abs() < 1e-7);
            config.set_picture_size(75.0).unwrap();
            let after = config.foreground;
            assert!(config.validate().is_ok(), "preset {id}");
            assert!((after.width / after.height - before.width / before.height).abs() < 1e-8);
            assert!((after.x + after.width / 2.0 - before.x - before.width / 2.0).abs() < 1e-8);
            assert!((after.y + after.height / 2.0 - before.y - before.height / 2.0).abs() < 1e-8);
            assert!((config.picture_size_percent() - 75.0).abs() < 1e-7);
        }
    }

    #[test]
    fn expanded_native_compositions_own_real_layers_and_clear_safely() {
        for (id, roles) in [
            (9, &["broadcastSide", "broadcastAccent", "broadcastFoot", "broadcastTop"][..]),
            (12, &["backdrop", "tint", "shadow"][..]),
            (13, &["diagonalLeft", "diagonalRight", "diagonalHighlight"][..]),
            (14, &["projectorMount", "projectorRail"][..]),
            (15, &["browserShell", "browserHeader", "browserDotRed", "browserDotAmber", "browserDotGreen"][..]),
            (16, &["tapeLeft", "tapeRight"][..]),
            (18, &["gridUpper", "gridRight", "gridLower", "gridLeft"][..]),
        ] {
            for landscape in [false, true] {
                let (mut p, mut mint) = fixture();
                if landscape { p.media[0].width = Some(1920); p.media[0].height = Some(1080); }
                let before = p.active().clip("c1").unwrap().clone();
                let config = Config::preset(id).unwrap();
                apply(&mut p, &mut mint, config, vec!["c1".into()], vec![]).unwrap();
                let pieces: Vec<_> = p.active().clips.iter().filter(|c| c.extra.get("canvasOwner") == Some(&json!("c1"))).collect();
                for role in roles {
                    assert!(pieces.iter().any(|c| c.extra.get("canvasRole").and_then(Value::as_str) == Some(*role)), "preset {id}: {role}");
                }
                assert_eq!(p.media.len(), 1);
                assert_no_canvas_headline(&p);
                clear(&mut p, vec!["c1".into()], vec![]).unwrap();
                assert!(p.active().clips.iter().all(|c| !c.extra.contains_key("canvasOwner")));
                let owner = p.active().clip("c1").unwrap();
                assert_eq!(owner.media_id, before.media_id);
                assert_eq!(owner.keys, before.keys);
            }
        }
        assert!(!ACTIVE_PRESETS.contains(&7) && !ACTIVE_PRESETS.contains(&8));
        assert!(Config::preset(7).is_some() && Config::preset(8).is_some());
    }

    #[test]
    fn picture_size_rejects_nonfinite_or_invalid_values_without_mutating() {
        let mut config = Config::preset(4).unwrap();
        let before = config.clone();
        for invalid in [f64::NAN, f64::INFINITY, -1.0, 0.0, 140.0] {
            assert!(config.set_picture_size(invalid).is_err());
            assert_eq!(config, before);
        }
    }

}
