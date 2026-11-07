// SPDX-License-Identifier: AGPL-3.0-or-later

use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};
use slint::ComponentHandle;

use crate::ui::{App, CloudGpu};

pub(crate) const SETTINGS_PAGE: i32 = 8;
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

enum Task {
    Save,
    Test,
    Submit(String),
}

impl Task {
    fn name(&self) -> &'static str {
        match self {
            Self::Save => "Save",
            Self::Test => "Test",
            Self::Submit(_) => "Submit",
        }
    }
}

pub(crate) fn wire(app: &App) {
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
}

pub(crate) fn can_submit(app: &App) -> bool {
    let gpu = app.global::<CloudGpu>();
    if gpu.get_busy() {
        return false;
    }
    if !gpu.get_configured() {
        gpu.set_connection_error(true);
        gpu.set_connection_note("Save your Salad connection in GPU Config first.".into());
        return false;
    }
    true
}

pub(crate) fn submit(app: &App, path: String) {
    run(app, Task::Submit(path));
}

fn run(app: &App, task: Task) {
    let gpu = app.global::<CloudGpu>();
    if gpu.get_busy() {
        return;
    }
    let is_job = matches!(task, Task::Submit(_));
    let is_save = matches!(task, Task::Save);
    let mut draft = (!is_job).then(|| {
        json!({
            "domain": gpu.get_domain().as_str(),
            "salad": gpu.get_salad_key().as_str(),
            "renderer": gpu.get_renderer_token().as_str(),
        })
        .to_string()
    });
    gpu.set_busy(true);
    if is_job {
        gpu.set_job_note("Preparing the saved project.".into());
        gpu.set_job_error(false);
        gpu.set_output_location("".into());
        gpu.set_progress(-1);
    } else {
        gpu.set_connection_error(false);
        gpu.set_connection_note(
            if is_save {
                "Saving credentials…"
            } else {
                "Testing connection…"
            }
            .into(),
        );
    }

    let weak = app.as_weak();
    std::thread::spawn(move || {
        let result = run_script(&task, draft.take(), |event| {
            let _ = weak.upgrade_in_event_loop(move |app| {
                let gpu = app.global::<CloudGpu>();
                let message = event["message"].as_str().unwrap_or_default();
                let failed = event["failed"].as_bool().unwrap_or(false);
                let location = event["location"].as_str().unwrap_or_default();
                if is_job {
                    gpu.set_job_note(message.into());
                    gpu.set_job_error(failed);
                    if let Some(progress) =
                        event["progress"].as_i64().filter(|p| (0..=100).contains(p))
                    {
                        gpu.set_progress(progress as i32);
                    }
                    if !location.is_empty() {
                        gpu.set_output_location(location.into());
                    }
                } else {
                    gpu.set_connection_note(message.into());
                    gpu.set_connection_error(failed);
                    if is_save && !failed && !location.is_empty() {
                        gpu.set_configured(true);
                        gpu.set_domain(location.into());
                        gpu.set_salad_key("".into());
                        gpu.set_renderer_token("".into());
                    }
                }
            });
        });
        let _ = weak.upgrade_in_event_loop(move |app| {
            let gpu = app.global::<CloudGpu>();
            if let Err(error) = result {
                if is_job {
                    gpu.set_job_note(error.into());
                    gpu.set_job_error(true);
                } else {
                    gpu.set_connection_note(error.into());
                    gpu.set_connection_error(true);
                }
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
    if let Task::Submit(project) = task {
        command.arg("-ProjectDir").arg(project);
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

    let mut reported = false;
    let mut explained_failure = false;
    let output = child.stdout.take().expect("piped GPU helper output");
    for line in BufReader::new(output).lines().map_while(Result::ok) {
        if let Ok(event) = serde_json::from_str::<Value>(&line) {
            if event["message"].is_string() {
                reported = true;
                explained_failure |= event["failed"].as_bool().unwrap_or(false);
                publish(event);
            }
        }
    }
    let status = child
        .wait()
        .map_err(|_| "Could not read the GPU helper exit status.")?;
    if (!status.success() && !explained_failure) || !reported {
        return Err(format!("The GPU helper stopped unexpectedly ({status})."));
    }
    Ok(())
}
