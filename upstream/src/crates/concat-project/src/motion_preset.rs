// SPDX-License-Identifier: AGPL-3.0-or-later

//! Inspector motion presets expressed as ordinary, undoable clip keys.

use crate::Command;
use crate::model::{Clip, ClipKind, KeyEase, KeyProperty, ranges};

/// The starter animations available from the native picture inspector.
#[derive(Clone, Copy, Debug)]
pub enum MotionPreset {
    /// Increase the clip's current scale by 18 percent.
    PushIn,
    /// Move the clip right by eight percent of the frame width.
    PanRight,
    /// Raise opacity from zero to the clip's current setting.
    FadeIn,
}

/// Why a motion preset cannot be placed on this clip.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PresetError {
    /// Presets apply to image and video clips.
    NeedsPicture,
    /// The clip must have at least two visible frames.
    TooShort,
    /// Applying the preset would compete with keys on the same property.
    AlreadyKeyed,
    /// The property's value is already at the preset's limit.
    NoChange,
}

impl MotionPreset {
    /// Build one undoable edit and return the timeline time of a frame in
    /// its range so the inspector can show the effect immediately.
    pub fn command(self, clip: &Clip, frame: f64) -> Result<(Command, f64), PresetError> {
        if !matches!(clip.kind, ClipKind::Video | ClipKind::Image) {
            return Err(PresetError::NeedsPicture);
        }
        if !frame.is_finite() || frame <= 0.0 || !clip.duration.is_finite() {
            return Err(PresetError::TooShort);
        }
        // The clip ends just after its last visible frame on a frame-aligned
        // edit. Use a key on a visible frame rather than on that boundary.
        let last = (clip.duration / frame - 1e-9).ceil() - 1.0;
        if !last.is_finite() || last < 1.0 {
            return Err(PresetError::TooShort);
        }
        let (property, first, final_value, end_frame) = match self {
            Self::PushIn => (
                KeyProperty::Scale,
                clip.scale,
                (clip.scale * 1.18).min(ranges::MAX_SCALE),
                last,
            ),
            Self::PanRight => (
                KeyProperty::OffsetX,
                clip.offset_x,
                (clip.offset_x + 0.08).min(ranges::MAX_OFFSET),
                last,
            ),
            Self::FadeIn => (
                KeyProperty::Opacity,
                0.0,
                clip.opacity,
                (0.1 * clip.duration / frame).ceil().min(last),
            ),
        };
        if clip.is_keyed(property) {
            return Err(PresetError::AlreadyKeyed);
        }
        if first == final_value {
            return Err(PresetError::NoChange);
        }
        let end = end_frame * frame / clip.duration;
        let commands = [
            (0.0, first, KeyEase::LINEAR),
            (end, final_value, KeyEase::IN_OUT),
        ]
        .into_iter()
        .map(|(at, value, ease)| Command::SetClipKey {
            clip_id: clip.id.clone(),
            property,
            at,
            value,
            ease,
        })
        .collect();
        let preview = clip.start + (end_frame / 2.0).round() * frame;
        Ok((Command::Batch { commands }, preview))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::NewMedia;
    use crate::model::MediaKind;
    use crate::{DocumentSettings, Editor, to_document};

    const FRAME: f64 = 1.0 / 30.0;

    fn apply_preset(clip: &mut Clip, preset: MotionPreset) -> f64 {
        let (Command::Batch { commands }, preview) = preset.command(clip, FRAME).unwrap() else {
            panic!("a preset must be one undo step");
        };
        assert_eq!(commands.len(), 2);
        for command in commands {
            let Command::SetClipKey {
                property,
                at,
                value,
                ease,
                ..
            } = command
            else {
                panic!("a preset must use existing project keys");
            };
            clip.set_key(property, at, value, ease);
        }
        preview
    }

    #[test]
    fn push_and_pan_start_at_existing_framing_and_preview_inside_the_clip() {
        let mut clip = Clip::blank("c1", "t1", ClipKind::Image, "picture", 2.0, 4.0);
        clip.scale = 1.5;
        clip.offset_x = -0.2;
        let preview = apply_preset(&mut clip, MotionPreset::PushIn);
        assert!(preview > 2.0 && preview < 6.0);
        assert_eq!(clip.value_at(KeyProperty::Scale, 0.0), 1.5);
        assert!((clip.value_at(KeyProperty::Scale, 1.0) - 1.77).abs() < 1e-9);
        assert_eq!(clip.offset_x, -0.2);

        apply_preset(&mut clip, MotionPreset::PanRight);
        assert_eq!(clip.value_at(KeyProperty::OffsetX, 0.0), -0.2);
        assert!((clip.value_at(KeyProperty::OffsetX, 1.0) + 0.12).abs() < 1e-9);
        assert_eq!(clip.keys_on(KeyProperty::Scale).count(), 2);
    }

    #[test]
    fn fade_holds_existing_opacity_and_preserves_existing_keys() {
        let mut clip = Clip::blank("c1", "t1", ClipKind::Video, "picture", 0.0, 10.0);
        clip.opacity = 0.6;
        let preview = apply_preset(&mut clip, MotionPreset::FadeIn);
        assert_eq!(preview, 0.5);
        assert_eq!(clip.value_at(KeyProperty::Opacity, 0.0), 0.0);
        assert!(clip.value_at(KeyProperty::Opacity, 0.05) > 0.0);
        assert_eq!(clip.value_at(KeyProperty::Opacity, 0.1), 0.6);
        assert_eq!(clip.value_at(KeyProperty::Opacity, 1.0), 0.6);
        assert_eq!(
            MotionPreset::FadeIn.command(&clip, FRAME).unwrap_err(),
            PresetError::AlreadyKeyed,
        );
    }

    #[test]
    fn preset_rejects_one_frame_and_audio_clips() {
        let short = Clip::blank("c1", "t1", ClipKind::Image, "still", 0.0, FRAME);
        assert_eq!(
            MotionPreset::PushIn.command(&short, FRAME).unwrap_err(),
            PresetError::TooShort,
        );
        let audio = Clip::blank("c2", "t1", ClipKind::Audio, "voice", 0.0, 2.0);
        assert_eq!(
            MotionPreset::FadeIn.command(&audio, FRAME).unwrap_err(),
            PresetError::NeedsPicture,
        );
    }

    #[test]
    fn preset_survives_document_round_trip_and_undo_removes_both_keys() {
        let mut editor = Editor::new();
        let media_id = editor
            .apply(Command::AddMedia {
                item: NewMedia {
                    path: "/clip.mp4".to_owned(),
                    name: "clip.mp4".to_owned(),
                    duration: Some(2.0),
                    kind: MediaKind::Video,
                    width: Some(1920),
                    height: Some(1080),
                    frame_rate: Some(30.0),
                    frame_rate_fraction: Some("30/1".to_owned()),
                    video_codec: Some("h264".to_owned()),
                    audio_codec: None,
                    has_audio: false,
                    audio_tracks: Vec::new(),
                    origin: None,
                    color_space: Default::default(),
                },
            })
            .unwrap()
            .created_id
            .unwrap();
        let track_id = editor.project().active().tracks[0].id.clone();
        let clip_id = editor
            .apply(Command::AddClip {
                media_id,
                track_id,
                start: 0.0,
                ripple: false,
            })
            .unwrap()
            .created_id
            .unwrap();
        let clip = editor.project().active().clip(&clip_id).unwrap();
        let (command, _) = MotionPreset::PushIn.command(clip, FRAME).unwrap();
        editor.apply(command).unwrap();
        let settings = DocumentSettings {
            name: "Motion fixture".to_owned(),
            width: 1920,
            height: 1080,
            rate_num: 30,
            rate_den: 1,
        };
        let doc = to_document(&settings, editor.project());
        let restored = Editor::from_document(&doc).unwrap();
        let saved = restored.project().active().clip(&clip_id).unwrap();
        assert_eq!(saved.keys_on(KeyProperty::Scale).count(), 2);
        assert_eq!(saved.value_at(KeyProperty::Scale, 1.0), 1.18);
        assert!(editor.undo());
        assert!(
            !editor
                .project()
                .active()
                .clip(&clip_id)
                .unwrap()
                .is_keyed(KeyProperty::Scale)
        );
    }
}
