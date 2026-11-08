// SPDX-License-Identifier: AGPL-3.0-or-later

use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};
use slint::ComponentHandle;

use crate::ui::{App, CloudGpu};

#[path = "cloud_gpu_queue.rs"]
mod queue;

pub(crate) const SETTINGS_PAGE: i32 = 8;
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Default)]
pub(crate) struct AbortState {
    pub aborted: bool,
    pub child: Option<std::process::Child>,
}

pub(crate) type AbortHandle = Arc<Mutex<AbortState>>;

pub(crate) fn new_abort_handle() -> AbortHandle {
    Arc::new(Mutex::new(AbortState::default()))
}

pub(crate) fn trigger_abort(handle: &AbortHandle) {
    if let Ok(mut guard) = handle.lock() {
        guard.aborted = true;
        if let Some(child) = guard.child.as_mut() {
            let _ = child.kill();
        }
    }
}

enum Task {
    Save,
    Test,
    OfflineBundle {
        project: String,
        destination: PathBuf,
    },
    Queue {
        operation: &'static str,
        job: queue::Job,
    },
}

impl Task {
    fn name(&self) -> &'static str {
        match self {
            Self::Save => "Save",
            Self::Test => "Test",
            Self::OfflineBundle { .. } => "OfflineBundle",
            Self::Queue { operation, .. } => operation,
        }
    }
}

pub(crate) fn wire(app: &App) -> slint::Timer {
    let gpu = app.global::<CloudGpu>();
    gpu.set_available(true);
    let saved = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .and_then(|path| fs::read_to_string(path.join("Concat/GPU/connection.json")).ok())
        .and_then(|text| serde_json::from_str::<Value>(text.trim_start_matches('\u{feff}')).ok());
    if let Some(saved) = saved {
        gpu.set_domain(saved["domain"].as_str().unwrap_or_default().into());
        gpu.set_configured(
            ["domain", "salad", "renderer"]
                .iter()
                .all(|key| saved[*key].as_str().is_some_and(|value| !value.is_empty())),
        );
    }
    let weak = app.as_weak();
    gpu.on_save(move || {
        if let Some(app) = weak.upgrade() {
            run(&app, Task::Save);
        }
    });
    let weak = app.as_weak();
    gpu.on_test(move || {
        if let Some(app) = weak.upgrade() {
            run(&app, Task::Test);
        }
    });
    queue::wire(app)
}

pub(crate) fn can_submit(app: &App) -> bool {
    app.global::<CloudGpu>().get_queue_available()
}

pub(crate) fn submit(app: &App, path: String) {
    app.global::<CloudGpu>().invoke_queue_add(path.into());
}

pub(crate) fn offline_zip_busy(app: &App) -> bool {
    app.global::<CloudGpu>().get_zip_busy()
}

/// This path deliberately never opens the queue or resolves Salad credentials.
/// The helper receives only a saved project directory and a user-chosen local
/// ZIP pathname. It snapshots the saved manifest and calls Concat-Bundle.
pub(crate) fn prepare_zip_only(app: &App, project: String, destination: PathBuf) {
    let gpu = app.global::<CloudGpu>();
    if gpu.get_zip_busy() {
        gpu.set_zip_open(true);
        return;
    }
    gpu.set_zip_busy(true);
    gpu.set_zip_open(true);
    gpu.set_zip_error(false);
    gpu.set_zip_progress(0);
    gpu.set_zip_note("Preparing local GPU ZIP (offline)…".into());
    gpu.set_zip_path(destination.to_string_lossy().to_string().into());

    let weak = app.as_weak();
    std::thread::spawn(move || {
        let mut saw_failure = false;
        let result = run_script(
            &Task::OfflineBundle { project, destination },
            None,
            None,
            |event| {
                saw_failure |= event["failed"].as_bool().unwrap_or(false);
                let _ = weak.upgrade_in_event_loop(move |app| {
                    let gpu = app.global::<CloudGpu>();
                    if let Some(progress) = event["progress"].as_i64().filter(|p| *p >= 0) {
                        gpu.set_zip_progress(progress.clamp(0, 100) as i32);
                    }
                    if let Some(message) = event["message"].as_str() {
                        gpu.set_zip_note(message.into());
                    }
                    if event["failed"].as_bool().unwrap_or(false) {
                        gpu.set_zip_error(true);
                    }
                    if let Some(path) = event["location"].as_str().filter(|s| !s.is_empty()) {
                        gpu.set_zip_path(path.into());
                    }
                });
            },
        );
        let _ = weak.upgrade_in_event_loop(move |app| {
            let gpu = app.global::<CloudGpu>();
            if let Err(error) = result {
                gpu.set_zip_error(true);
                gpu.set_zip_note(error.into());
            } else if !saw_failure {
                gpu.set_zip_progress(100);
            }
            gpu.set_zip_busy(false);
        });
    });
}

fn run(app: &App, task: Task) {
    let gpu = app.global::<CloudGpu>();
    if gpu.get_busy() {
        return;
    }
    let is_save = matches!(task, Task::Save);
    let draft = json!({
        "domain": gpu.get_domain().as_str(),
        "salad": gpu.get_salad_key().as_str(),
        "renderer": gpu.get_renderer_token().as_str(),
    })
    .to_string();
    gpu.set_busy(true);
    gpu.set_connection_error(false);
    gpu.set_metrics_note("".into());
    gpu.set_connection_note(
        if is_save {
            "Saving credentials…"
        } else {
            "Testing connection…"
        }
        .into(),
    );

    let weak = app.as_weak();
    std::thread::spawn(move || {
        let result = run_script(&task, Some(draft), None, |event| {
            let _ = weak.upgrade_in_event_loop(move |app| {
                let gpu = app.global::<CloudGpu>();
                let message = event["message"].as_str().unwrap_or_default();
                let failed = event["failed"].as_bool().unwrap_or(false);
                let location = event["location"].as_str().unwrap_or_default();
                gpu.set_connection_note(message.into());
                gpu.set_connection_error(failed);
                if let Some(metrics) = event["metrics"].as_str().filter(|s| !s.is_empty()) {
                    gpu.set_metrics_note(metrics.into());
                }
                if is_save && !failed && !location.is_empty() {
                    gpu.set_configured(true);
                    gpu.set_domain(location.into());
                    gpu.set_salad_key("".into());
                    gpu.set_renderer_token("".into());
                }
            });
        });
        let _ = weak.upgrade_in_event_loop(move |app| {
            let gpu = app.global::<CloudGpu>();
            if let Err(error) = result {
                gpu.set_connection_note(error.into());
                gpu.set_connection_error(true);
            }
            gpu.set_busy(false);
        });
    });
}

struct TemporaryScript(PathBuf);

impl Drop for TemporaryScript {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn run_script(
    task: &Task,
    draft: Option<String>,
    abort: Option<AbortHandle>,
    mut publish: impl FnMut(Value),
) -> Result<(), String> {
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "The Windows clock could not be read.")?
        .as_nanos();
    let path = std::env::temp_dir().join(format!("concat-gpu-{}-{stamp}.ps1", std::process::id()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|_| "Could not create the temporary GPU helper.")?;
    let script = TemporaryScript(path);
    file.write_all(include_bytes!("concat_gpu_client.ps1"))
        .map_err(|_| "Could not write the temporary GPU helper.")?;
    drop(file);

    let mut command = Command::new("powershell.exe");
    command
        .creation_flags(CREATE_NO_WINDOW)
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
        ])
        .arg(&script.0)
        .args(["-Operation", task.name()])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    if let Task::OfflineBundle { project, destination } = task {
        command.arg("-ProjectDir").arg(project);
        command.arg("-BundlePath").arg(destination);
    }
    if let Task::Queue { operation, job } = task {
        command.args(["-Job", &job.remote, "-SnapshotID", &job.id]);
        if *operation == "Prepare" {
            command.args(["-ProjectDir", &job.project]);
        } else {
            command.args(["-JobDomain", &job.domain]);
        }
    }
    let mut child = command
        .spawn()
        .map_err(|_| "Could not launch Windows PowerShell.")?;
    let mut input = child.stdin.take().expect("piped GPU helper input");
    if let Some(draft) = draft {
        if input.write_all(draft.as_bytes()).is_err() {
            let _ = child.kill();
            let _ = child.wait();
            return Err("Could not send settings to the GPU helper.".into());
        }
    }
    drop(input);

    let output = child.stdout.take().expect("piped GPU helper output");
    // Keep ownership explicit across both abort and non-abort paths: Rust
    // cannot assume two separate Option matches take the same branch.
    let mut owned_child = Some(child);

    if let Some(ref handle) = abort {
        let mut guard = handle.lock().unwrap();
        if guard.aborted {
            if let Some(mut interrupted) = owned_child.take() {
                let _ = interrupted.kill();
                let _ = interrupted.wait();
            }
            return Err("The transfer was cancelled.".into());
        }
        guard.child = owned_child.take();
    }

    let mut reported = false;
    let mut explained_failure = false;
    for line in BufReader::new(output).lines().map_while(Result::ok) {
        if let Ok(event) = serde_json::from_str::<Value>(&line) {
            if event["message"].is_string() {
                reported = true;
                explained_failure |= event["failed"].as_bool().unwrap_or(false);
                publish(event);
            }
        }
    }

    let status = if let Some(ref handle) = abort {
        let mut guard = handle.lock().unwrap();
        if guard.aborted {
            if let Some(mut child) = guard.child.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
            return Err("The transfer was cancelled.".into());
        }
        if let Some(mut child) = guard.child.take() {
            child.wait().map_err(|_| "Could not read the GPU helper exit status.")?
        } else {
            return Err("The transfer process was lost.".into());
        }
    } else {
        owned_child
            .as_mut()
            .ok_or("The transfer process was lost.")?
            .wait()
            .map_err(|_| "Could not read the GPU helper exit status.")?
    };

    if (!status.success() && !explained_failure) || !reported {
        return Err(format!("The GPU helper stopped unexpectedly ({status})."));
    }
    Ok(())
}
