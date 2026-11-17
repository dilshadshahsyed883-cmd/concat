// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Concat contributors

//! Home-only, fail-closed project deletion. Removing a Recent entry is a
//! different operation (see `projects::forget`) and never touches the disk.
//! This module deliberately has no recursive delete or permanent fallback.

use std::collections::HashSet;
use std::fs::{self, Metadata};
use std::path::{Component, Path, PathBuf};

use crate::projects;

#[derive(Clone, Debug)]
pub struct DeleteTarget {
    /// The canonical directory shown in the confirmation and recycled.
    pub path: PathBuf,
    /// The exact name the user must type to authorize the operation.
    pub name: String,
}

fn unsafe_link(meta: &Metadata) -> bool {
    if meta.file_type().is_symlink() {
        return true;
    }
    // Windows directory junctions, mount points, cloud placeholders and
    // other reparse points are not all reported by FileType::is_symlink.
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if meta.file_attributes() & 0x400 != 0 {
            return true;
        }
    }
    false
}

fn same_or_inside(path: &Path, root: &Path) -> bool {
    #[cfg(windows)]
    {
        let spelling = |p: &Path| p.to_string_lossy().replace('/', "\\").to_lowercase();
        let path = spelling(path);
        let root = spelling(root);
        return path == root || path.starts_with(&(root.trim_end_matches('\\').to_owned() + "\\"));
    }
    #[cfg(not(windows))]
    {
        path == root || path.starts_with(root)
    }
}

fn check_path(path: &Path, config: &Path) -> Result<PathBuf, String> {
    if !path.is_absolute()
        || path.components().any(|part| matches!(part, Component::ParentDir | Component::CurDir))
    {
        return Err("Delete Project requires an absolute, unambiguous folder path".into());
    }
    // Never treat a volume root, its immediate child, a user/desktop root,
    // or any directory containing application configuration as a project.
    if path.parent().and_then(Path::parent).is_none() {
        return Err("Delete Project refuses a filesystem root or volume-level folder".into());
    }
    for part in path.ancestors() {
        let meta = fs::symlink_metadata(part)
            .map_err(|e| format!("Cannot verify {}: {e}", part.display()))?;
        if unsafe_link(&meta) {
            return Err(format!(
                "Delete Project refuses a symlink, junction or reparse point: {}",
                part.display()
            ));
        }
    }
    let root = fs::canonicalize(path)
        .map_err(|e| format!("Cannot resolve project directory {}: {e}", path.display()))?;
    if !root.is_dir() {
        return Err(format!("Project is not a directory: {}", root.display()));
    }
    let protected = [
        std::env::home_dir(),
        std::env::home_dir().map(|p| p.join("Desktop")),
        std::env::home_dir().map(|p| p.join("Documents")),
        Some(config.to_owned()),
        config.parent().map(Path::to_path_buf),
    ];
    for dir in protected.into_iter().flatten() {
        if let Ok(real) = fs::canonicalize(dir) {
            if same_or_inside(&real, &root) {
                return Err(format!(
                    "Delete Project refuses a home, Desktop, application or shared parent directory: {}",
                    root.display()
                ));
            }
        }
    }
    // Refuse an embedded project (or a project already inside another
    // project) rather than recycling a folder with unrelated ownership.
    for ancestor in root.ancestors().skip(1) {
        if projects::is_project(ancestor) {
            return Err("Nested projects cannot be deleted from Recent Projects".into());
        }
    }
    Ok(root)
}

fn scan_tree(root: &Path) -> Result<(), String> {
    let mut stack = vec![root.to_path_buf()];
    while let Some(folder) = stack.pop() {
        for item in fs::read_dir(&folder)
            .map_err(|e| format!("Cannot inspect project folder {}: {e}", folder.display()))?
        {
            let item = item.map_err(|e| format!("Cannot inspect project contents: {e}"))?;
            let path = item.path();
            let meta = fs::symlink_metadata(&path)
                .map_err(|e| format!("Cannot inspect {}: {e}", path.display()))?;
            if unsafe_link(&meta) {
                return Err(format!(
                    "Project contains a symlink, junction or reparse point; unlink it manually before deletion: {}",
                    path.display()
                ));
            }
            if meta.is_dir() {
                if projects::is_project(&path) {
                    return Err(format!(
                        "Project contains another project; deletion is blocked: {}",
                        path.display()
                    ));
                }
                stack.push(path);
            } else if !meta.is_file() {
                return Err(format!(
                    "Project contains an unsupported filesystem entry: {}",
                    path.display()
                ));
            } else if item.file_name().to_string_lossy().contains(".saving.") {
                return Err("Project has an in-progress save; close it before deletion".into());
            }
        }
    }
    Ok(())
}

fn refers_to(value: &serde_json::Value, other: &Path, target: &Path) -> bool {
    match value {
        serde_json::Value::String(s) => {
            let p = Path::new(s);
            // Paths, not arbitrary titles/IDs. Relative source media and
            // outputs are interpreted relative to the owning project.
            if !p.is_absolute()
                && !s.starts_with("../")
                && !s.starts_with("..\\")
                && !s.starts_with("./")
                && !s.starts_with(".\\")
            {
                return false;
            }
            let path = if p.is_absolute() { p.to_path_buf() } else { other.join(p) };
            let resolved = fs::canonicalize(&path).unwrap_or(path);
            same_or_inside(&resolved, target)
        }
        serde_json::Value::Array(values) => values.iter().any(|v| refers_to(v, other, target)),
        serde_json::Value::Object(values) => values.values().any(|v| refers_to(v, other, target)),
        _ => false,
    }
}

// Cloud GPU Queue stores the project path independently from the active
// editor. A queued, uploading or rendering job is an external consumer of
// this directory; closing the editor does not make its source disposable.
#[cfg(windows)]
fn protect_gpu_queue(target: &Path) -> Result<(), String> {
    let Some(local) = std::env::var_os("LOCALAPPDATA") else {
        return Err("Cannot verify active GPU Queue without LOCALAPPDATA".into());
    };
    let queue = PathBuf::from(local)
        .join("Concat").join("GPU").join("Queue").join("queue.json");
    let bytes = match fs::read(&queue) {
        Ok(bytes) => bytes,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(format!("Cannot inspect GPU Queue {}: {e}", queue.display())),
    };
    let data: serde_json::Value = serde_json::from_slice(&bytes)
        .map_err(|e| format!("Cannot safely read GPU Queue state: {e}"))?;
    let jobs = data.get("jobs").and_then(serde_json::Value::as_array)
        .ok_or("GPU Queue has no valid jobs list; deletion blocked")?;
    for job in jobs {
        let path = job.get("project").and_then(serde_json::Value::as_str)
            .ok_or("GPU Queue has an invalid project path; deletion blocked")?;
        let phase = job.get("phase").and_then(serde_json::Value::as_str)
            .ok_or("GPU Queue has an invalid phase; deletion blocked")?;
        if matches!(phase, "Completed" | "Cancelled") {
            continue;
        }
        let path = fs::canonicalize(path).unwrap_or_else(|_| PathBuf::from(path));
        if same_or_inside(&path, target) || same_or_inside(target, &path) {
            return Err(format!(
                "Project has an active or queued GPU job ({phase}). Cancel/finish the queue job first"
            ));
        }
    }
    Ok(())
}

#[cfg(not(windows))]
fn protect_gpu_queue(_target: &Path) -> Result<(), String> {
    Ok(())
}

fn protect_other_projects(config: &Path, target: &Path, selected: &str) -> Result<(), String> {
    let mut paths = Vec::new();
    for recent in projects::recents(config) {
        if recent.info.path != selected {
            paths.push(PathBuf::from(recent.info.path));
        }
    }
    // Include projects no longer in Recents but in the same parent folder.
    // These are common sources of reusable media and nested output folders.
    if let Some(parent) = target.parent() {
        for entry in fs::read_dir(parent)
            .map_err(|e| format!("Cannot verify neighboring projects: {e}"))?
        {
            let entry = entry.map_err(|e| format!("Cannot inspect neighbor: {e}"))?;
            if entry.file_type().map_err(|e| e.to_string())?.is_dir()
                && projects::is_project(&entry.path())
            {
                paths.push(entry.path());
            }
        }
    }
    let mut seen = HashSet::new();
    for path in paths {
        if !path.exists() {
            continue;
        }
        let real = fs::canonicalize(&path)
            .map_err(|e| format!("Cannot verify another project {}: {e}", path.display()))?;
        if !seen.insert(real.clone()) {
            continue;
        }
        if real == target {
            if path != target && path.to_string_lossy() != selected {
                return Err("Another project entry references the same physical folder; deletion blocked".into());
            }
            continue;
        }
        if same_or_inside(&real, target) || same_or_inside(target, &real) {
            return Err(format!(
                "Another project shares or nests the selected directory: {}",
                real.display()
            ));
        }
        if !projects::is_project(&real) {
            continue;
        }
        let document = projects::read_document(&real.to_string_lossy())
            .map_err(|e| format!("Cannot check shared references in {}: {e}", real.display()))?
            .ok_or_else(|| format!("Other project has no readable manifest: {}", real.display()))?;
        if refers_to(&document, &real, target) {
            return Err(format!(
                "Another project references media/output inside this folder: {}",
                real.display()
            ));
        }
    }
    Ok(())
}

/// Checks the exact card/manifest identity, physical location and known
/// sharing relationships. Called when the dialog opens AND immediately
/// before recycling, so a stale dialog cannot authorize a different folder.
pub fn prepare(
    config: &Path,
    selected_path: &str,
    expected_name: &str,
    active_path: Option<&str>,
) -> Result<DeleteTarget, String> {
    let recent = projects::recents(config).into_iter()
        .find(|r| r.info.path == selected_path)
        .ok_or("Project is no longer in Recent Projects; nothing was deleted")?;
    if recent.info.name != expected_name {
        return Err("Project was renamed since confirmation; please review it again".into());
    }
    if active_path.is_some_and(|p| projects::same_path(p, selected_path)) {
        return Err("Close the active project (and save changes) before deleting it".into());
    }
    let root = check_path(Path::new(selected_path), config)?;
    let manifest = projects::manifest_path(&root);
    let meta = fs::symlink_metadata(&manifest)
        .map_err(|e| format!("Project manifest is missing or inaccessible: {e}"))?;
    if !meta.is_file() || unsafe_link(&meta) {
        return Err("Project manifest is not an ordinary file".into());
    }
    let info = projects::open(&root.to_string_lossy())?;
    if info.name != expected_name {
        return Err("Project manifest name changed; please reopen the delete confirmation".into());
    }
    scan_tree(&root)?;
    protect_other_projects(config, &root, selected_path)?;
    protect_gpu_queue(&root)?;
    Ok(DeleteTarget { path: root, name: expected_name.to_owned() })
}

/// Recycles the directory *only* after rechecking. A failure never calls
/// `remove_dir_all` or `forget`, and leaves the Recent card in place.
pub fn recycle(
    config: &Path,
    selected_path: &str,
    expected_name: &str,
    active_path: Option<&str>,
) -> Result<(), String> {
    let target = prepare(config, selected_path, expected_name, active_path)?;
    recycle_directory(&target.path)?;
    match fs::symlink_metadata(&target.path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!(
            "Recycle Bin returned but the project folder cannot be verified: {e}. Recent entry kept."
        )),
        Ok(_) => return Err("Project is still on disk after recycling; Recent entry kept".into()),
    }
    projects::forget(config, selected_path).map_err(|e| format!(
        "Project moved to Recycle Bin, but Recent metadata could not be updated: {e}"
    ))
}

#[cfg(windows)]
fn recycle_directory(path: &Path) -> Result<(), String> {
    // Use the Windows shell through the .NET FileIO RecycleOption API.
    // There is no permanent-delete code path, even if the volume cannot
    // recycle (network drive, long path, policy, permissions, etc).
    // Pass the path as a child-process environment value, NOT interpolated
    // into PowerShell source, so quotes and non-ASCII names are safe.
    const SCRIPT: &str = r#"
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName Microsoft.VisualBasic
$target = [Environment]::GetEnvironmentVariable('CONCAT_DELETE_PROJECT_TARGET')
if ([String]::IsNullOrWhiteSpace($target)) { throw 'Missing project directory' }
[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
    $target,
    [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
    [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin,
    [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException)
"#;
    // Rust canonicalization on Windows produces verbatim paths (\\?\\).
    // Windows PowerShell 5.1/.NET FileIO is not consistently compatible with
    // that prefix. Give the shell the ordinary absolute path only when it is
    // short enough for its legacy APIs; otherwise fail rather than strip a
    // prefix and accidentally target a different file.
    let text = path.to_string_lossy();
    let shell_path = if let Some(local) = text.strip_prefix(r"\\?\\") {
        if local.len() >= 248 || local.starts_with("UNC\\") {
            return Err("Windows Recycle Bin cannot safely handle this extended-length or UNC project path; no permanent delete attempted".into());
        }
        local.to_owned()
    } else {
        text.into_owned()
    };
    let output = std::process::Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", SCRIPT])
        .env("CONCAT_DELETE_PROJECT_TARGET", shell_path)
        .output()
        .map_err(|e| format!("Cannot start Windows Recycle Bin service: {e}"))?;
    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "Windows did not recycle this project (no permanent delete attempted): {}",
            detail.trim()
        ));
    }
    Ok(())
}

#[cfg(not(windows))]
fn recycle_directory(_path: &Path) -> Result<(), String> {
    Err("Delete Project requires the Windows Recycle Bin. Remove from Recent is still available.".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn setup(name: &str) -> (TempDir, PathBuf, projects::ProjectInfo) {
        let root = tempfile::tempdir().unwrap();
        let config = root.path().join("config");
        let projects_dir = root.path().join("my-projects");
        fs::create_dir_all(&projects_dir).unwrap();
        let info = projects::create(&projects_dir.to_string_lossy(), name, 1920, 1080, 30, 1).unwrap();
        projects::remember(&config, &info).unwrap();
        (root, config, info)
    }

    #[test]
    fn remove_from_recent_keeps_original_files() {
        let (_scratch, config, info) = setup("Keep files");
        projects::forget(&config, &info.path).unwrap();
        assert!(Path::new(&info.path).join("concat.json").is_file());
        assert!(projects::recents(&config).is_empty());
    }

    #[test]
    fn rejects_missing_or_unlisted_and_mismatched_names() {
        let (_scratch, config, info) = setup("Real name");
        assert!(prepare(&config, &info.path, "Wrong name", None).is_err());
        assert!(prepare(&config, "/not-a-listed-project", "Real name", None).is_err());
        assert!(prepare(&config, &info.path, "Real name", Some(&info.path)).is_err());
        assert!(Path::new(&info.path).exists());
    }

    #[test]
    fn refuses_nested_projects_and_symlinks() {
        let (_scratch, config, info) = setup("Parent");
        let nested = projects::create(&info.path, "Child", 1080, 1920, 30, 1).unwrap();
        assert!(prepare(&config, &info.path, &info.name, None).is_err());
        assert!(Path::new(&nested.path).exists());
    }

    #[cfg(unix)]
    #[test]
    fn refuses_symlinked_input_inside_project_without_following_it() {
        use std::os::unix::fs::symlink;
        let (scratch, config, info) = setup("Linked");
        let external = scratch.path().join("outside.mov");
        fs::write(&external, b"original").unwrap();
        symlink(&external, Path::new(&info.path).join("external.mov")).unwrap();
        assert!(prepare(&config, &info.path, &info.name, None).is_err());
        assert_eq!(fs::read(&external).unwrap(), b"original");
    }

    #[test]
    fn blocks_other_project_using_shared_media() {
        let (scratch, config, info) = setup("Shared owner");
        let other = projects::create(
            &scratch.path().join("my-projects").to_string_lossy(),
            "Consumer", 1920, 1080, 30, 1
        ).unwrap();
        let shared = Path::new(&info.path).join("clip.mov");
        fs::write(&shared, b"original").unwrap();
        let mut doc = projects::read_document(&other.path).unwrap().unwrap();
        doc["media"] = serde_json::json!([{ "path": shared.to_string_lossy() }]);
        projects::save(&other.path, &doc).unwrap();
        assert!(prepare(&config, &info.path, &info.name, None).is_err());
        assert_eq!(fs::read(&shared).unwrap(), b"original");
    }

    #[test]
    fn accepts_external_media_refs_but_never_traverses_external_folders() {
        let (scratch, config, info) = setup("External refs");
        let external = scratch.path().join("external.mov");
        fs::write(&external, b"original").unwrap();
        let mut doc = projects::read_document(&info.path).unwrap().unwrap();
        doc["media"] = serde_json::json!([{ "path": external.to_string_lossy() }]);
        projects::save(&info.path, &doc).unwrap();
        assert!(prepare(&config, &info.path, &info.name, None).is_ok());
        assert_eq!(fs::read(&external).unwrap(), b"original");
    }

    #[test]
    fn refuses_missing_manifest_and_invalid_project() {
        let (_scratch, config, info) = setup("Missing");
        fs::remove_file(Path::new(&info.path).join("concat.json")).unwrap();
        assert!(prepare(&config, &info.path, &info.name, None).is_err());
        assert!(Path::new(&info.path).exists());
    }

    #[cfg(not(windows))]
    #[test]
    fn non_windows_cannot_permanently_delete_as_fallback() {
        let (_scratch, config, info) = setup("No fallback");
        assert!(recycle(&config, &info.path, &info.name, None).is_err());
        assert!(Path::new(&info.path).exists());
        assert_eq!(projects::recents(&config).len(), 1);
    }

    #[cfg(windows)]
    #[test]
    fn windows_recycles_real_project_and_keeps_external_media() {
        let (scratch, config, info) = setup("日本語 — Recycle test");
        let outside = scratch.path().join("external.wav");
        fs::write(&outside, b"untouched").unwrap();
        recycle(&config, &info.path, &info.name, None).expect("Windows Recycle Bin");
        assert!(!Path::new(&info.path).exists());
        assert!(projects::recents(&config).is_empty());
        assert_eq!(fs::read(outside).unwrap(), b"untouched");
    }
}
