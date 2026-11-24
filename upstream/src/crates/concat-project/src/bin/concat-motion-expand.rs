// SPDX-License-Identifier: AGPL-3.0-or-later
//! Headless project-key generator used by the CUDA worker. No renderer or GPU
//! dependencies: this is the desktop project's native motion math only.
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut document: serde_json::Value = serde_json::from_reader(std::io::stdin().lock())?;
    concat_project::motion_wire::expand_document(&mut document, false)
        .map_err(std::io::Error::other)?;
    serde_json::to_writer(std::io::stdout().lock(), &document)?;
    Ok(())
}
