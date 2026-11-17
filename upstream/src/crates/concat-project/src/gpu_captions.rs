// SPDX-License-Identifier: AGPL-3.0-or-later
//! Editable GPU caption artifacts. This module validates speech timings, never guesses them.
use crate::Command;
use crate::model::{CaptionWord, Clip, TextStyle};
use serde::Deserialize;

/// Versioned result returned by the configured Concat worker.
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Artifact {
    /// Supported result schema.
    pub version: u32,
    /// Source clip in the uploaded project snapshot.
    pub source_clip_id: String,
    /// Timeline start of the source clip, in milliseconds.
    pub timeline_start_ms: u64,
    /// The selected caption look, independent from recognition cache identity.
    pub preset: String,
    /// Phrase cues, timed relative to source clip playback.
    pub cues: Vec<ArtifactCue>,
}
/// One phrase and its speech-derived word intervals.
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactCue {
    /// Inclusive cue start relative to the source clip.
    pub start_ms: u64,
    /// Exclusive cue end relative to the source clip.
    pub end_ms: u64,
    /// Displayed phrase.
    pub text: String,
    /// Word intervals relative to the source clip (not the cue).
    #[serde(default)]
    pub words: Vec<CaptionWord>,
}
impl Artifact {
    /// Validate a bounded artifact and translate into ordinary editable text clips.
    /// All commands belong in one atomic batch/Undo operation.
    pub fn commands(&self) -> Result<Vec<Command>, String> {
        if self.version != 1
            || self.source_clip_id.is_empty()
            || self.cues.is_empty()
            || self.cues.len() > 100_000
            || self.timeline_start_ms > 86_400_000
        {
            return Err("Invalid GPU caption artifact version, source or cue count".into());
        }
        if !matches!(self.preset.as_str(), "purple-word" | "yellow-bar") {
            return Err("Unsupported GPU caption preset".into());
        }
        let mut previous = 0;
        self.cues
            .iter()
            .map(|cue| {
                if cue.start_ms < previous
                    || cue.end_ms <= cue.start_ms
                    || cue.end_ms > 86_400_000
                    || cue.text.trim().is_empty()
                    || cue.text.len() > 8192
                {
                    return Err("Invalid or overlapping GPU caption interval".into());
                }
                previous = cue.end_ms;
                let purple = self.preset == "purple-word";
                let mut style = TextStyle {
                    content: cue.text.to_uppercase(),
                    font_family: "Inter".into(),
                    font_weight: 900.0,
                    font_size: 0.053,
                    color: if purple { "#ffffff" } else { "#000000" }.into(),
                    background: if purple { "" } else { "#ffff00" }.into(),
                    stroke_width: if purple { 0.004 } else { 0.0 },
                    shadow: purple,
                    karaoke: purple,
                    karaoke_background: if purple { "#a020f0" } else { "" }.into(),
                    ..TextStyle::default()
                };
                if !cue.words.is_empty() {
                    style.karaoke_words = cue
                        .words
                        .iter()
                        .map(|word| {
                            Ok(CaptionWord {
                                text: word.text.to_uppercase(),
                                start_ms: word
                                    .start_ms
                                    .checked_sub(cue.start_ms)
                                    .ok_or("Word starts before its phrase")?,
                                end_ms: word
                                    .end_ms
                                    .checked_sub(cue.start_ms)
                                    .ok_or("Word ends before its phrase")?,
                            })
                        })
                        .collect::<Result<Vec<_>, String>>()?;
                    style.validate_caption_words((cue.end_ms - cue.start_ms) as f64 / 1000.0)?;
                } else if purple {
                    return Err(
                        "Purple word highlighting requires actual speech word timestamps".into(),
                    );
                }
                Ok(Command::AddTextClip {
                    track_id: None,
                    above: true,
                    start: (self.timeline_start_ms + cue.start_ms) as f64 / 1000.0,
                    duration: Some((cue.end_ms - cue.start_ms) as f64 / 1000.0),
                    offset_y: Some(if purple { 0.28 } else { -0.35 }),
                    style: Some(style),
                })
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn artifact_import_preserves_exact_words_and_source_placement() {
        let result:Artifact=serde_json::from_value(serde_json::json!({"version":1,"sourceClipId":"voice1","timelineStartMs":2000,"preset":"purple-word","cues":[{"startMs":500,"endMs":1500,"text":"But happened","words":[{"text":"But","startMs":500,"endMs":750},{"text":"happened","startMs":900,"endMs":1500}]}]})).unwrap();
        let commands = result.commands().unwrap();
        let Command::AddTextClip {
            start,
            style: Some(style),
            ..
        } = &commands[0]
        else {
            panic!("native text command")
        };
        assert_eq!(*start, 2.5);
        assert_eq!(style.content, "BUT HAPPENED");
        assert_eq!(style.karaoke_words[1].start_ms, 400);
        assert_eq!(style.karaoke_background, "#a020f0");
    }
    #[test]
    fn purple_rejects_guessed_missing_and_overlapping_words() {
        let mut result = Artifact {
            version: 1,
            source_clip_id: "v1".into(),
            timeline_start_ms: 0,
            preset: "purple-word".into(),
            cues: vec![ArtifactCue {
                start_ms: 0,
                end_ms: 1000,
                text: "A B".into(),
                words: vec![],
            }],
        };
        assert!(result.commands().is_err());
        result.cues[0].words = vec![
            CaptionWord {
                text: "A".into(),
                start_ms: 0,
                end_ms: 700,
            },
            CaptionWord {
                text: "B".into(),
                start_ms: 500,
                end_ms: 1000,
            },
        ];
        assert!(result.commands().is_err());
        result.preset = "yellow-bar".into();
        result.cues[0].words.clear();
        assert!(result.commands().is_ok());
    }
}

/// Convert an accurately timed phrase into private render windows, preserving
/// one editable project clip. Speech gaps deliberately have no active word.
pub fn render_segments(clip: &Clip) -> Result<Vec<Clip>, String> {
    let Some(mut style) = clip
        .text
        .clone()
        .filter(|s| !s.karaoke_background.is_empty())
    else {
        return Ok(vec![clip.clone()]);
    };
    if style.karaoke_words.is_empty() {
        if let Some(words) = clip.extra.get("captionWords") {
            style.karaoke_words = serde_json::from_value(words.clone())
                .map_err(|e| format!("Invalid caption word data: {e}"))?;
        }
    }
    style.validate_caption_words(clip.duration)?;
    let mut boundaries = vec![(0.0, None)];
    for (index, word) in style.karaoke_words.iter().enumerate() {
        boundaries.push((word.start_ms as f64 / 1000.0, Some(index)));
        boundaries.push((word.end_ms as f64 / 1000.0, None));
    }
    boundaries.push((clip.duration, None));
    boundaries.sort_by(|a, b| a.0.total_cmp(&b.0));
    let mut result = Vec::new();
    for window in boundaries.windows(2) {
        let (start, active) = window[0];
        let end = window[1].0;
        if end <= start {
            continue;
        }
        let mut segment = clip.clone();
        segment.rewindow_keys(clip.duration, start, end);
        segment.start = clip.start + start;
        segment.duration = end - start;
        let mut text = style.clone();
        text.karaoke_word = active;
        segment.text = Some(text);
        result.push(segment);
    }
    Ok(result)
}

#[cfg(test)]
mod render_tests {
    use super::*;
    use crate::model::ClipKind;
    #[test]
    fn phrase_windows_use_exact_word_edges_and_leave_speech_gaps_unhighlighted() {
        let mut clip = Clip::blank("cap1", "T4", ClipKind::Text, "phrase", 2.0, 1.0);
        clip.text = Some(TextStyle {
            content: "BUT HAPPENED".into(),
            karaoke_background: "#a020f0".into(),
            karaoke_words: vec![
                CaptionWord {
                    text: "BUT".into(),
                    start_ms: 100,
                    end_ms: 300,
                },
                CaptionWord {
                    text: "HAPPENED".into(),
                    start_ms: 450,
                    end_ms: 900,
                },
            ],
            ..TextStyle::default()
        });
        let before = serde_json::to_value(&clip).unwrap();
        let windows = render_segments(&clip).unwrap();
        assert_eq!(windows.len(), 5);
        assert_eq!(windows[0].text.as_ref().unwrap().karaoke_word, None);
        assert_eq!(windows[1].text.as_ref().unwrap().karaoke_word, Some(0));
        assert_eq!(windows[3].text.as_ref().unwrap().karaoke_word, Some(1));
        assert!((windows[1].start - 2.1).abs() < 1e-9);
        assert!((windows[1].duration - 0.2).abs() < 1e-9);
        assert_eq!(before, serde_json::to_value(&clip).unwrap());
        assert!(
            !serde_json::to_string(&windows[1])
                .unwrap()
                .contains("karaokeWord\"")
        );
        clip.text.as_mut().unwrap().karaoke_words.clear();
        assert!(render_segments(&clip).is_err());
    }
}
