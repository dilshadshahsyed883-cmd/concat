//! Execute the real C Builder tests without linking FFmpeg, ONNX, or a GUI.
//! Probe IO is deliberately unavailable: these tests cover cached planning and native document writing.
extern crate self as concat_host;

pub mod media {
    use concat_project::model::MediaKind;
    pub struct VideoStreamInfo {
        pub width: u32, pub height: u32, pub frame_rate: f64,
        pub frame_rate_fraction: String, pub codec: String,
    }
    pub struct AudioStreamInfo { pub codec: String }
    pub struct MediaSummary {
        pub kind: MediaKind, pub duration: Option<f64>,
        pub video: Option<VideoStreamInfo>, pub audio: Option<AudioStreamInfo>,
    }
    pub fn probe(_: &str) -> Result<MediaSummary, String> {
        Err("Media probing is not available in the source-only test harness".into())
    }
}
#[path = "../../../upstream/src/crates/concat/src/c_builder.rs"]
pub mod c_builder;

#[path = "../../../upstream/src/crates/concat/src/c_builder_used.rs"]
pub mod c_builder_used;

// Exercise the real Transition inspector planner and native undo/serialization
// with the same project-only source harness; no desktop linking is needed.
#[path = "../../../upstream/src/crates/concat/src/studio/transition_duration.rs"]
pub mod transition_duration;
