// SPDX-License-Identifier: AGPL-3.0-or-later
//! Editable canvas compositions made from ordinary source, shape and text clips.
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
    pub headline: Label,
    pub badge: Label,
}
fn color(s: &str) -> bool {
    matches!(s.len(), 7 | 9) && s.starts_with('#') && s[1..].bytes().all(|b| b.is_ascii_hexdigit())
}
impl Config {
    pub fn preset(number: u32) -> Option<Self> {
        if !(1..=3).contains(&number) {
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
            enabled: true,
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
        Some(Self {
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
            headline,
            badge,
        })
    }
    pub fn validate(&self) -> Result<(), String> {
        if self.version != 1 || !(1..=3).contains(&self.preset) || !self.foreground.validate() {
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
        ] {
            if !value.is_finite() || value < 0.0 || value > max {
                return Err(format!("Invalid canvas {name}"));
            }
        }
        for value in [
            &self.border_color,
            &self.background_color,
            &self.background_tint,
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
/// Apply to explicit unlocked visual targets. No media IO or source mutation.
pub fn apply(
    project: &mut Project,
    mint: &mut IdMint,
    config: Config,
    targets: Vec<String>,
    locked: Vec<String>,
) -> Result<Outcome, CommandError> {
    config.validate().map_err(error)?;
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
        let base: Clip = serde_json::from_value(baseline.clone())
            .map_err(|_| error("Canvas original snapshot is invalid"))?;
        if base.mask.is_some() {
            return Err(error(
                "Remove the existing authored mask before applying a canvas layout",
            ));
        }
        staged
            .active_mut()
            .clips
            .retain(|c| c.extra.get("canvasOwner") != Some(&json!(&id)));
        let bg_lane = track(&mut staged, mint, &original.track_id, "backdrop", true);
        let tint_lane = track(&mut staged, mint, &original.track_id, "tint", true);
        let frame_lane = track(&mut staged, mint, &original.track_id, "frame", true);
        let banner_lane = track(&mut staged, mint, &original.track_id, "banner", false);
        let badge_bg_lane = track(
            &mut staged,
            mint,
            &original.track_id,
            "badgeBackground",
            false,
        );
        let text_lane = track(&mut staged, mint, &original.track_id, "headline", false);
        let badge_text_lane = track(&mut staged, mint, &original.track_id, "badge", false);
        let mut layers = Vec::new();
        if config.background != "none" {
            let mut bg = base.clone();
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
        if config.border_width > 0.0 {
            layers.push(rectangle(
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
            ));
        }
        // Labels keep independent canvas anchors while only the original media moves.
        for (mut piece, lane) in label(mint, &original, &banner_lane, "headline", &config.headline)
            .into_iter()
            .map(|c| {
                (
                    c.clone(),
                    if c.kind == ClipKind::Text {
                        &text_lane
                    } else {
                        &banner_lane
                    },
                )
            })
        {
            Arc::make_mut(&mut piece).track_id = lane.clone();
            layers.push(piece);
        }
        for (mut piece, lane) in label(mint, &original, &badge_bg_lane, "badge", &config.badge)
            .into_iter()
            .map(|c| {
                (
                    c.clone(),
                    if c.kind == ClipKind::Text {
                        &badge_text_lane
                    } else {
                        &badge_bg_lane
                    },
                )
            })
        {
            Arc::make_mut(&mut piece).track_id = lane.clone();
            layers.push(piece);
        }
        let foreground = staged
            .active_mut()
            .clip_mut(&id)
            .ok_or(CommandError::MediaGone)?;
        foreground.scale = scale;
        let (x, y) = actual.center();
        foreground.offset_x = x;
        foreground.offset_y = y;
        foreground.background = Some(Background::None);
        if config.radius > 0.0 {
            let mut mask = ClipMask::of(MaskShape::Rectangle);
            mask.width = sw / short;
            mask.height = sh / short;
            mask.feather = 0.0;
            mask.roundness = (config.radius * h / scale / (short * fit / 2.0)).clamp(0.0, 1.0);
            foreground.mask = Some(mask);
        } else {
            foreground.mask = None;
        }
        foreground.extra.insert(
            "canvasPreset".into(),
            serde_json::to_value(&config).map_err(|e| error(e.to_string()))?,
        );
        foreground.extra.insert("canvasOriginal".into(), baseline);
        staged.active_mut().clips.extend(layers);
        created = Some(id);
    }
    *project = staged;
    Ok(Outcome {
        created_id: created,
        applied: true,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{MediaItem, MediaKind};
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
    #[test]
    fn independent_presets_validate_and_round_trip() {
        for n in 1..=3 {
            let p = Config::preset(n).unwrap();
            p.validate().unwrap();
            assert_eq!(
                p,
                Config::from_json(&serde_json::to_string(&p).unwrap()).unwrap()
            );
            assert!(p.headline.text.is_empty());
            assert!(!p.badge.enabled);
        }
    }
    #[test]
    fn native_layers_preserve_media_timing_and_reapplication_replaces_owned_layers() {
        let (mut p, mut m) = fixture();
        let mut c = Config::preset(2).unwrap();
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
        assert!(source.scale < 1.0);
        assert!(
            p.active().clips.iter().any(|c| c.kind == ClipKind::Text
                && c.text.as_ref().unwrap().content == "EDITABLE HEADLINE")
        );
        apply(&mut p, &mut m, c, vec!["c1".into()], vec![]).unwrap();
        assert_eq!(count, p.active().clips.len());
        assert_eq!(p.media.len(), 1);
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
        for number in 1..=3 {
            for portrait in [true, false] {
                let (mut p, mut mint) = fixture();
                p.media[0].path = "source.mp4".into();
                if !portrait {
                    p.media[0].width = Some(1920);
                    p.media[0].height = Some(1080);
                }
                let mut config = Config::preset(number).unwrap();
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
}
