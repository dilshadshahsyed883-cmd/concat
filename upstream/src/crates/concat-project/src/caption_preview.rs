// SPDX-License-Identifier: AGPL-3.0-or-later
//! Non-destructive caption style staging, shared by cards, monitor and explicit Apply.
use crate::model::{CaptionWord, Clip, ClipKind, TextStyle, Timeline};

/// A pending look, separate from the saved document and undo stack.
#[derive(Clone, Debug)]
pub struct StagedCaption {
    /// One ordinary, native-renderable title clip.
    pub clip: Clip,
    /// True only for a clearly labelled, synthetic sample.
    pub sample: bool,
    /// True when the highlight follows speech-derived word intervals.
    pub exact_words: bool,
    /// An explanatory limitation for an existing untimed phrase.
    pub message: Option<String>,
}

/// A selected text lane, or the most representative visible caption lane.
/// Generated canvas headlines never become implicit caption targets merely by outnumbering cues.
pub fn caption_lane(timeline: &Timeline, selected: Option<&str>) -> Option<String> {
    if let Some(clip) = selected.and_then(|id| timeline.clip(id)).filter(|c| {
        c.kind == ClipKind::Text
            && !c.extra.contains_key("canvasOwner")
            && timeline.track(&c.track_id).is_some_and(|t| t.visible)
    }) {
        return Some(clip.track_id.clone());
    }
    let mut counts = std::collections::BTreeMap::<String, (usize, usize)>::new();
    for clip in &timeline.clips {
        if clip.kind != ClipKind::Text
            || clip.extra.contains_key("canvasOwner")
            || !timeline.track(&clip.track_id).is_some_and(|t| t.visible)
        {
            continue;
        }
        let known = clip.extra.contains_key("gpuCaptionSource")
            || clip.extra.contains_key("captionWords")
            || clip
                .text
                .as_ref()
                .is_some_and(|s| !s.karaoke_words.is_empty());
        let count = counts.entry(clip.track_id.clone()).or_default();
        count.0 += usize::from(known);
        count.1 += 1;
    }
    counts
        .into_iter()
        .max_by_key(|(_, count)| (count.0 > 0, count.0, count.1))
        .map(|(id, _)| id)
}

/// Caption cues eligible for live audition, in chronological order.
/// The lane chooser and the interval index must agree about generated headlines:
/// a canvas-owned title is never a speech cue even when it shares the caption lane.
pub fn caption_candidates(
    timeline: &Timeline,
    selected: Option<&str>,
) -> Vec<std::sync::Arc<Clip>> {
    let lane = caption_lane(timeline, selected);
    let mut clips: Vec<_> = timeline
        .clips
        .iter()
        .filter(|clip| {
            clip.kind == ClipKind::Text
                && !clip.extra.contains_key("canvasOwner")
                && lane.as_deref() == Some(clip.track_id.as_str())
                && timeline.track(&clip.track_id).is_some_and(|t| t.visible)
        })
        .cloned()
        .collect();
    clips.sort_by(|a, b| a.start.total_cmp(&b.start));
    clips
}

/// Copy a preset onto the real caption, retaining text and exact word timings.
/// This returns the same style used by explicit Apply; it never modifies `source`.
pub fn style_for_clip(
    source: &Clip,
    template: &TextStyle,
    preset_id: &str,
) -> Result<TextStyle, String> {
    let original = source.text.as_ref().ok_or("Choose a caption clip")?;
    let mut style = template.clone();
    let uppercase = matches!(
        preset_id,
        "cuda-caption.preset-1-purple-word" | "cuda-caption.preset-2-yellow-bar"
    );
    style.content = if uppercase {
        original.content.to_uppercase()
    } else {
        original.content.clone()
    };
    style.karaoke_words = original.karaoke_words.clone();
    if style.karaoke_words.is_empty() {
        if let Some(words) = source.extra.get("captionWords") {
            style.karaoke_words = serde_json::from_value(words.clone())
                .map_err(|e| format!("Invalid caption word timings: {e}"))?;
        }
    }
    if uppercase {
        for word in &mut style.karaoke_words {
            word.text = word.text.to_uppercase();
        }
    }
    style.karaoke_word = None;
    if !style.karaoke_words.is_empty() {
        style.validate_caption_words(source.duration)?;
    }
    if (style.karaoke || !style.karaoke_background.is_empty()) && style.karaoke_words.is_empty() {
        return Err("Word timing unavailable: preview shows the real phrase without animated highlighting. Generate or import exact word times before applying this preset.".into());
    }
    Ok(style)
}

/// Stage an actual caption without changing its placement or speech intervals.
/// Missing word timing is reported and never replaced with synthetic speech timing.
pub fn stage_actual(source: &Clip, template: &TextStyle, preset_id: &str) -> StagedCaption {
    let mut clip = source.clone();
    let (style, message) = match style_for_clip(source, template, preset_id) {
        Ok(style) => (style, None),
        Err(message) => {
            let mut static_template = template.clone();
            static_template.karaoke_background.clear();
            static_template.karaoke = false;
            let style = style_for_clip(source, &static_template, preset_id).unwrap_or_else(|_| {
                let mut style = static_template;
                style.content = source
                    .text
                    .as_ref()
                    .map(|s| s.content.clone())
                    .unwrap_or_default();
                style.karaoke_words.clear();
                style
            });
            (style, Some(message))
        }
    };
    let exact_words = !style.karaoke_words.is_empty();
    clip.text = Some(style);
    if preset_id == "cuda-caption.preset-2-yellow-bar" {
        clip.offset_y = -0.35;
    }
    StagedCaption {
        clip,
        sample: false,
        exact_words,
        message,
    }
}

/// A deterministic sample cycle, controlled solely by the current playhead.
/// These intervals are demo animation, never transcribed or saved as speech alignment.
pub fn stage_sample(
    template: &TextStyle,
    preset_id: &str,
    at: f64,
    offset_y: Option<f64>,
) -> StagedCaption {
    let at = if at.is_finite() { at.max(0.0) } else { 0.0 };
    let mut clip = Clip::blank(
        "__caption_preview_sample",
        "__caption_preview",
        ClipKind::Text,
        "Sample caption",
        (at / 2.4).floor() * 2.4,
        2.4,
    );
    clip.offset_y = offset_y.unwrap_or(0.35);
    let mut style = template.clone();
    style.content = "Sample caption preview".into();
    style.karaoke_words = vec![
        CaptionWord {
            text: "Sample".into(),
            start_ms: 100,
            end_ms: 650,
        },
        CaptionWord {
            text: "caption".into(),
            start_ms: 800,
            end_ms: 1400,
        },
        CaptionWord {
            text: "preview".into(),
            start_ms: 1600,
            end_ms: 2300,
        },
    ];
    style.karaoke_word = None;
    clip.text = Some(style.clone());
    let mut staged = stage_actual(&clip, &style, preset_id);
    staged.sample = true;
    staged.exact_words = false;
    staged.message = Some("Sample caption — demo timing only; project unchanged".into());
    staged
}

/// A short, readable card label. Full preset names remain separate below the image.
pub fn card_sample(style: &TextStyle) -> TextStyle {
    let mut sample = style.clone();
    sample.content = "Hello".into();
    // Card typography is enlarged proportionally, preserving stroke/padding ratios.
    let factor = 0.22 / sample.font_size.max(0.01);
    sample.font_size = 0.22;
    sample.stroke_width = (sample.stroke_width * factor).min(0.03);
    sample.background_padding_x = (sample.background_padding_x * factor).min(0.07);
    sample.background_padding_y = (sample.background_padding_y * factor).min(0.04);
    sample.background_radius = (sample.background_radius * factor).min(0.08);
    sample.tracking *= factor;
    sample.max_width = 0.88;
    sample.max_height = 0.0;
    sample.karaoke_words = vec![CaptionWord {
        text: "Hello".into(),
        start_ms: 0,
        end_ms: 1000,
    }];
    sample.karaoke_word = if sample.karaoke || !sample.karaoke_background.is_empty() {
        Some(0)
    } else {
        None
    };
    sample
}

#[cfg(test)]
mod tests {
    use super::*;
    fn phrase() -> Clip {
        let mut clip = Clip::blank("cap", "T4", ClipKind::Text, "But happened", 4.0, 2.0);
        clip.offset_x = 0.1;
        clip.offset_y = 0.2;
        clip.text = Some(TextStyle {
            content: "But happened".into(),
            karaoke_words: vec![
                CaptionWord {
                    text: "But".into(),
                    start_ms: 100,
                    end_ms: 400,
                },
                CaptionWord {
                    text: "happened".into(),
                    start_ms: 850,
                    end_ms: 1800,
                },
            ],
            ..Default::default()
        });
        clip
    }
    #[test]
    fn selection_and_cancel_are_document_neutral() {
        let source = phrase();
        let before = serde_json::to_value(&source).unwrap();
        let purple = TextStyle {
            karaoke: true,
            karaoke_background: "#a020f0".into(),
            ..Default::default()
        };
        let preview = stage_actual(&source, &purple, "cuda-caption.preset-1-purple-word");
        assert_eq!(serde_json::to_value(&source).unwrap(), before);
        assert_eq!(preview.clip.start, 4.0);
        assert_eq!(preview.clip.duration, 2.0);
        assert_eq!(preview.clip.offset_x, 0.1);
        assert_eq!(preview.clip.offset_y, 0.2);
        assert_eq!(preview.clip.text.as_ref().unwrap().content, "BUT HAPPENED");
        assert_eq!(
            preview.clip.text.as_ref().unwrap().karaoke_words[1].start_ms,
            850
        );
        assert!(preview.exact_words);
        assert!(!preview.sample);
        let segments = crate::gpu_captions::render_segments(&preview.clip).unwrap();
        assert!(
            segments
                .iter()
                .any(|c| c.start == 4.4 && c.text.as_ref().unwrap().karaoke_word.is_none())
        );
    }
    #[test]
    fn explicit_apply_is_one_undo_and_preserves_project_round_trip() {
        let mut project = crate::model::Project::new();
        let source = phrase();
        project
            .active_mut()
            .clips
            .push(std::sync::Arc::new(source.clone()));
        let settings = crate::DocumentSettings {
            name: "Preview".into(),
            width: 1920,
            height: 1080,
            rate_num: 30,
            rate_den: 1,
        };
        let document = crate::to_document(&settings, &project);
        let mut editor = crate::Editor::from_document(&document).unwrap();
        let purple = TextStyle {
            karaoke: true,
            karaoke_background: "#a020f0".into(),
            ..Default::default()
        };
        let preview = stage_actual(&source, &purple, "cuda-caption.preset-1-purple-word");
        assert!(!editor.can_undo());
        assert_eq!(editor.to_document(&settings), document);
        editor
            .apply(crate::Command::Batch {
                commands: vec![crate::Command::UpdateClip {
                    clip_id: "cap".into(),
                    patch: crate::commands::ClipPatch {
                        text: Some(preview.clip.text.clone()),
                        ..Default::default()
                    },
                }],
            })
            .unwrap();
        let saved = editor.to_document(&settings);
        let reopened = crate::Editor::from_document(&saved).unwrap();
        assert_eq!(
            reopened.project().active().clip("cap").unwrap().text,
            preview.clip.text
        );
        assert!(editor.undo());
        assert_eq!(editor.to_document(&settings), document);
        assert!(!editor.can_undo());
        assert!(editor.redo());
        assert_eq!(editor.to_document(&settings), saved);
    }

    #[test]
    fn samples_are_explicit_and_scrub_deterministic() {
        let style = TextStyle {
            karaoke_background: "#a020f0".into(),
            ..Default::default()
        };
        let first = stage_sample(&style, "cuda-caption.preset-1-purple-word", 2.7, Some(0.25));
        let same = stage_sample(&style, "cuda-caption.preset-1-purple-word", 2.7, Some(0.25));
        assert_eq!(first.clip, same.clip);
        assert!(first.sample);
        assert!(!first.exact_words);
        assert!(first.message.unwrap().contains("demo timing"));
        assert_eq!(first.clip.start, 2.4);
        assert_eq!(first.clip.offset_y, 0.25);
    }
    #[test]
    fn actual_untimed_words_never_receive_demo_intervals() {
        let mut clip = phrase();
        clip.text.as_mut().unwrap().karaoke_words.clear();
        let style = TextStyle {
            karaoke_background: "#a020f0".into(),
            ..Default::default()
        };
        let preview = stage_actual(&clip, &style, "cuda-caption.preset-1-purple-word");
        assert!(preview.message.is_some());
        assert!(preview.clip.text.as_ref().unwrap().karaoke_words.is_empty());
        assert!(
            preview
                .clip
                .text
                .as_ref()
                .unwrap()
                .karaoke_background
                .is_empty()
        );
        assert!(style_for_clip(&clip, &style, "cuda-caption.preset-1-purple-word").is_err());
    }
    #[test]
    fn implicit_caption_lane_excludes_generated_headlines_and_prefers_timed_captions() {
        let mut project = crate::model::Project::new();
        let mut actual = phrase();
        actual.track_id = "T4".into();
        project.active_mut().clips.push(std::sync::Arc::new(actual));
        for i in 0..500 {
            let mut banner = Clip::blank(
                format!("banner{i}"),
                "T2",
                ClipKind::Text,
                "Headline",
                i as f64,
                1.0,
            );
            banner
                .extra
                .insert("canvasOwner".into(), serde_json::json!("visual"));
            project.active_mut().clips.push(std::sync::Arc::new(banner));
        }
        assert_eq!(caption_lane(project.active(), None).as_deref(), Some("T4"));
        assert_eq!(
            caption_lane(project.active(), Some("banner0")).as_deref(),
            Some("T4")
        );
        project
            .active_mut()
            .tracks
            .iter_mut()
            .find(|t| t.id == "T4")
            .unwrap()
            .visible = false;
        assert_eq!(caption_lane(project.active(), None), None);
        let mut untimed = Clip::blank("ordinary", "T1", ClipKind::Text, "Plain caption", 0.0, 2.0);
        untimed.text = Some(TextStyle::default());
        project
            .active_mut()
            .clips
            .push(std::sync::Arc::new(untimed));
        assert_eq!(caption_lane(project.active(), None).as_deref(), Some("T1"));
    }

    #[test]
    fn live_candidates_never_audition_canvas_headlines_on_the_caption_lane() {
        let mut project = crate::model::Project::new();
        let speech = phrase();
        let mut headline = speech.clone();
        headline.id = "canvas-headline".into();
        headline.start = 4.5;
        headline
            .extra
            .insert("canvasOwner".into(), serde_json::json!("video"));
        headline.text.as_mut().unwrap().content = "SPORTS HEADLINE".into();
        project
            .active_mut()
            .clips
            .extend([std::sync::Arc::new(headline), std::sync::Arc::new(speech)]);
        let candidates = caption_candidates(project.active(), Some("canvas-headline"));
        let active = candidates
            .iter()
            .rev()
            .find(|c| c.start <= 5.0 && 5.0 < c.start + c.duration);
        assert_eq!(active.map(|c| c.id.as_str()), Some("cap"));
        assert_eq!(candidates.len(), 1);
        project
            .active_mut()
            .tracks
            .iter_mut()
            .find(|t| t.id == "T4")
            .unwrap()
            .visible = false;
        assert!(caption_candidates(project.active(), None).is_empty());
    }

    #[test]
    fn card_samples_are_short_and_use_the_real_look() {
        let original = TextStyle {
            content: "A very long sample caption that would be clipped".into(),
            font_family: "Inter".into(),
            font_weight: 900.0,
            color: "#000000".into(),
            background: "#ffff00".into(),
            ..Default::default()
        };
        let card = card_sample(&original);
        assert_eq!(card.content, "Hello");
        assert_eq!(card.font_family, original.font_family);
        assert_eq!(card.background, original.background);
        assert_eq!(card.font_weight, 900.0);
        assert!(card.max_width < 1.0);
    }
}
