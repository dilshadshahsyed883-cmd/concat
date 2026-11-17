//! Compiles and runs the real Concat host project filesystem code, without
//! FFmpeg, GPU, or any desktop executable. Tests include Windows Recycle Bin.
#[path = "../../../upstream/src/crates/concat-host/src/projects.rs"]
pub mod projects;

#[path = "../../../upstream/src/crates/concat-host/src/project_delete.rs"]
pub mod project_delete;
