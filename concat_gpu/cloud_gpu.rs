// SPDX-License-Identifier: AGPL-3.0-or-later
// Native Concat GPU handoff, additive to upstream's unmodified edit model.
// This module is embedded in the real Concat Windows editor, not a new editor.

#[cfg(windows)]
pub(crate) fn launch(project_dir: &str) -> Result<(), String> {
    use std::io::Write;
    use std::time::{SystemTime, UNIX_EPOCH};
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_nanos();
    let script = std::env::temp_dir().join(format!(
        "concat-gpu-{}-{stamp}.ps1",
        std::process::id()
    ));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&script)
        .map_err(|error| error.to_string())?;
    file.write_all(include_str!("concat_gpu_client.ps1").as_bytes())
        .map_err(|error| error.to_string())?;
    file.flush().map_err(|error| error.to_string())?;

    // Both credentials remain user-local and are never passed as arguments,
    // written in Git, or exposed to the native editor process.
    // A visible PowerShell window provides upload/progress/error feedback.
    // The original Concat editor stays usable while it uploads.
    std::process::Command::new("powershell.exe")
        .arg("-NoLogo")
        .arg("-NoProfile")
        .arg("-ExecutionPolicy")
        .arg("Bypass")
        .arg("-NoExit")
        .arg("-File")
        .arg(&script)
        .arg("-ProjectDir")
        .arg(project_dir)
        .spawn()
        .map_err(|error| error.to_string())?;
    Ok(())
}
