// SPDX-License-Identifier: AGPL-3.0-or-later

use std::cell::RefCell;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::Value;
use slint::{ComponentHandle, ModelRc, VecModel};

use super::{Task, run_script};
use crate::ui::{App, CloudGpu, GpuQueueRow};

#[path = "gpu_queue_model.rs"]
mod model;
pub(super) use model::Job;
use model::{Phase, Queue};

#[derive(Clone, Copy, PartialEq)]
enum Action {
    Prepare,
    Upload,
    Status,
}

impl Action {
    fn operation(self) -> &'static str {
        match self {
            Self::Prepare => "Prepare",
            Self::Upload => "Upload",
            Self::Status => "Status",
        }
    }
}

enum Event {
    Update(String, Action, Value),
    Finished(String, Action, Result<(), String>),
}

struct Controller {
    root: PathBuf,
    _lock: File,
    data: Queue,
    tx: Sender<Event>,
    rx: Receiver<Event>,
    preparing: bool,
    uploading: bool,
    polling: bool,
    next_poll: Instant,
    poll_index: usize,
    storage_ok: bool,
    note: String,
    changed: bool,
}

impl Controller {
    fn open() -> io::Result<Self> {
        let base = std::env::var_os("LOCALAPPDATA")
            .ok_or_else(|| io::Error::other("Windows local application data is unavailable"))?;
        let root = PathBuf::from(base).join("Concat").join("GPU").join("Queue");
        fs::create_dir_all(&root)?;
        let lock = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .open(root.join("queue.lock"))?;
        lock.try_lock()
            .map_err(|_| io::Error::other("GPU Queue is open in another Concat window"))?;
        let mut data = match fs::read(root.join("queue.json")) {
            Ok(bytes) => serde_json::from_slice::<Queue>(&bytes)?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => Queue::default(),
            Err(error) => return Err(error),
        };
        let valid_id = |id: &str| {
            id.strip_prefix("concat-cuda-").is_some_and(|tail| {
                tail.len() == 16 && tail.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
        };
        if data
            .jobs
            .iter()
            .any(|job| !valid_id(&job.id) || !valid_id(&job.remote))
        {
            return Err(io::Error::other("Queue contains an invalid job identifier"));
        }
        data.recover();
        let (tx, rx) = mpsc::channel();
        Ok(Self {
            root,
            _lock: lock,
            data,
            tx,
            rx,
            preparing: false,
            uploading: false,
            polling: false,
            next_poll: Instant::now(),
            poll_index: 0,
            storage_ok: true,
            note: "Queue stopped. Click Start Queue when ready.".into(),
            changed: true,
        })
    }

    fn save(&mut self) -> bool {
        let result = (|| -> io::Result<()> {
            let stage = self.root.join("queue.json.tmp");
            let mut file = File::create(&stage)?;
            serde_json::to_writer(&mut file, &self.data)?;
            file.flush()?;
            file.sync_all()?;
            drop(file);
            fs::rename(stage, self.root.join("queue.json"))?;
            Ok(())
        })();
        self.changed = true;
        if let Err(error) = result {
            self.data.started = false;
            self.storage_ok = false;
            self.note = format!("Queue stopped because its state could not be saved: {error}");
            return false;
        }
        true
    }

    fn next_id(&self) -> String {
        let mut id = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos() as u64;
        loop {
            let candidate = format!("concat-cuda-{id:016x}");
            if !self
                .data
                .jobs
                .iter()
                .any(|job| job.id == candidate || job.remote == candidate)
                && !self.root.join(&candidate).exists()
            {
                return candidate;
            }
            id = id.wrapping_add(1);
        }
    }

    fn add(&mut self, project: &str) {
        if !self.storage_ok {
            return;
        }
        let path = Path::new(project);
        let manifest = ["concat.json", "wolfcut.json"]
            .iter()
            .map(|name| path.join(name))
            .find(|file| file.is_file());
        let id = self.next_id();
        let snapshot = self.root.join(&id);
        let result = (|| -> io::Result<()> {
            let manifest =
                manifest.ok_or_else(|| io::Error::other("Saved project manifest not found"))?;
            fs::create_dir(&snapshot)?;
            fs::copy(manifest, snapshot.join("concat.json"))?;
            Ok(())
        })();
        if let Err(error) = result {
            let _ = fs::remove_dir_all(snapshot);
            self.note = format!("Could not snapshot project: {error}");
            self.changed = true;
            return;
        }
        self.data.jobs.push(Job {
            remote: id.clone(),
            id,
            label: path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or(project)
                .into(),
            project: project.into(),
            domain: String::new(),
            phase: Phase::Preparing,
            attempted: false,
            progress: 0,
            detail: "Preparing a local snapshot.".into(),
            output: String::new(),
        });
        self.save();
    }

    fn control(&mut self, action: &str, id: &str) {
        if !self.storage_ok {
            return;
        }
        match action {
            "start" => {
                self.data.started = true;
                self.note = "Queue started. Uploads run one at a time.".into();
                self.changed = true;
                return;
            }
            "stop" => {
                self.data.started = false;
                self.note = "Queue stopped. Any current upload can finish.".into();
                self.changed = true;
                return;
            }
            _ => {}
        }
        let Some(index) = self.data.jobs.iter().position(|job| job.id == id) else {
            return;
        };
        let phase = self.data.jobs[index].phase;
        match action {
            "retry" if phase.can_retry() => {
                let new_id = self.next_id();
                let job = &mut self.data.jobs[index];
                job.phase = match phase {
                    Phase::PrepareFailed => Phase::Preparing,
                    Phase::StatusFailed => Phase::Queued,
                    Phase::RenderFailed => {
                        job.remote = new_id;
                        job.attempted = false;
                        job.progress = 0;
                        job.domain.clear();
                        Phase::Waiting
                    }
                    _ => Phase::Waiting,
                };
                job.detail = "Ready to retry.".into();
            }
            "cancel" if self.can_cancel(index) => {
                self.data.jobs[index].phase = Phase::Cancelled;
                self.data.jobs[index].detail = "Cancelled before submission.".into();
            }
            "delete" if self.can_delete(index) => {
                self.data.jobs.remove(index);
                if self.save() {
                    // This removes only the desktop's private queue snapshot and manifest.
                    let _ = fs::remove_file(self.root.join(format!("{id}.zip")));
                    let _ = fs::remove_dir_all(self.root.join(id));
                }
                return;
            }
            "up" | "down" if self.can_move(index) => {
                let other = if action == "up" {
                    (0..index).rev().find(|&i| self.can_move(i))
                } else {
                    (index + 1..self.data.jobs.len()).find(|&i| self.can_move(i))
                };
                if let Some(other) = other {
                    self.data.jobs.swap(index, other);
                }
            }
            _ => return,
        }
        self.save();
    }

    fn can_move(&self, index: usize) -> bool {
        matches!(
            self.data.jobs[index].phase,
            Phase::Preparing | Phase::Waiting
        )
    }

    fn can_cancel(&self, index: usize) -> bool {
        let job = &self.data.jobs[index];
        !job.attempted
            && matches!(
                job.phase,
                Phase::Waiting | Phase::PrepareFailed | Phase::UploadFailed
            )
    }

    fn can_delete(&self, index: usize) -> bool {
        self.can_cancel(index)
            || matches!(
                self.data.jobs[index].phase,
                Phase::Cancelled | Phase::Completed | Phase::RenderFailed | Phase::StatusFailed
            )
    }

    fn launch(&self, action: Action, job: Job) {
        let tx = self.tx.clone();
        std::thread::spawn(move || {
            let id = job.id.clone();
            let result = run_script(
                &Task::Queue {
                    operation: action.operation(),
                    job,
                },
                None,
                |value| {
                    let _ = tx.send(Event::Update(id.clone(), action, value));
                },
            );
            let _ = tx.send(Event::Finished(id, action, result));
        });
    }

    fn tick(&mut self, app: &App) {
        while let Ok(event) = self.rx.try_recv() {
            self.event(event);
        }
        if self.storage_ok {
            if !self.preparing {
                if let Some(job) = self
                    .data
                    .jobs
                    .iter()
                    .find(|job| job.phase == Phase::Preparing)
                {
                    self.preparing = true;
                    self.launch(Action::Prepare, job.clone());
                }
            }
            if self.data.started
                && !self.uploading
                && !self
                    .data
                    .jobs
                    .iter()
                    .any(|job| matches!(job.phase, Phase::Preparing | Phase::Waiting))
            {
                self.data.started = false;
                self.note = "Upload queue finished. Click Start Queue for new jobs.".into();
                self.changed = true;
            }
            let gpu = app.global::<CloudGpu>();
            if !gpu.get_busy() {
                self.start_upload();
                self.poll();
            }
        }
        if self.changed {
            self.publish(app);
        }
    }

    fn start_upload(&mut self) {
        if self.uploading {
            return;
        }
        let Some(job) = self.data.next_upload() else {
            return;
        };
        let id = job.id.clone();
        let domain = fs::read(self.root.join("../connection.json"))
            .ok()
            .and_then(|bytes| {
                serde_json::from_str::<Value>(
                    String::from_utf8_lossy(&bytes).trim_start_matches('\u{feff}'),
                )
                .ok()
            })
            .and_then(|value| value["domain"].as_str().map(str::to_owned));
        let Some(domain) = domain else {
            self.data.started = false;
            self.note = "Save a GPU connection, then click Start Queue.".into();
            self.changed = true;
            return;
        };
        let job = self
            .data
            .jobs
            .iter_mut()
            .find(|job| job.id == id)
            .expect("queued job");
        if job.domain.is_empty() {
            job.domain = domain;
        }
        job.phase = Phase::Uploading;
        job.detail = "Checking the worker.".into();
        let task = job.clone();
        if self.save() {
            self.uploading = true;
            self.launch(Action::Upload, task);
        }
    }

    fn poll(&mut self) {
        if self.polling || Instant::now() < self.next_poll {
            return;
        }
        let jobs: Vec<_> = self
            .data
            .jobs
            .iter()
            .filter(|job| job.phase.submitted())
            .collect();
        if !jobs.is_empty() {
            let job = jobs[self.poll_index % jobs.len()].clone();
            self.poll_index = self.poll_index.wrapping_add(1);
            self.polling = true;
            self.launch(Action::Status, job);
        }
    }

    fn event(&mut self, event: Event) {
        match event {
            Event::Update(id, action, value) => {
                let Some(job) = self.data.jobs.iter_mut().find(|job| job.id == id) else {
                    return;
                };
                let detail = value["message"].as_str().unwrap_or_default().to_owned();
                let phase = value["phase"].as_str().unwrap_or_default();
                let failure = value["failed"].as_bool().unwrap_or(false);
                if let Some(progress) = value["progress"].as_i64().filter(|p| (0..=100).contains(p))
                {
                    job.progress = progress as i32;
                }
                job.detail = detail.clone();
                if let Some(output) = value["location"].as_str().filter(|value| !value.is_empty()) {
                    job.output = output.into();
                }
                match (action, phase, failure) {
                    (Action::Prepare, "prepared", _) => job.phase = Phase::Waiting,
                    (Action::Prepare, _, true) => job.phase = Phase::PrepareFailed,
                    (Action::Upload, "attempted", _) => job.attempted = true,
                    (Action::Upload, "uploaded", _) => job.phase = Phase::Queued,
                    (Action::Upload, _, true) => {
                        self.data.fail_upload(&id, phase == "offline", detail);
                        self.note =
                            "Queue paused. Check the worker and click Start Queue to continue."
                                .into();
                    }
                    (Action::Status, "queued", _) => job.phase = Phase::Queued,
                    (Action::Status, "rendering", _) => job.phase = Phase::Rendering,
                    (Action::Status, "completed", _) => job.phase = Phase::Completed,
                    (Action::Status, "render_failed", _) => job.phase = Phase::RenderFailed,
                    (Action::Status, "offline", _) => {
                        if self.data.started {
                            self.data.started = false;
                            self.note =
                                "Worker disconnected. Click Start Queue to resume uploads.".into();
                        }
                    }
                    (Action::Status, _, true) => job.phase = Phase::StatusFailed,
                    _ => {
                        self.changed = true;
                        return;
                    }
                }
                self.save();
            }
            Event::Finished(id, action, result) => {
                match action {
                    Action::Prepare => self.preparing = false,
                    Action::Status => {
                        self.polling = false;
                        self.next_poll = Instant::now() + Duration::from_secs(4);
                    }
                    Action::Upload => self.uploading = false,
                }
                let Some(job) = self.data.jobs.iter_mut().find(|job| job.id == id) else {
                    return;
                };
                match action {
                    Action::Upload if job.phase == Phase::Uploading => {
                        job.attempted = true;
                        let detail = result.err().unwrap_or_else(|| {
                            "Upload confirmation lost. Resume to check the worker.".into()
                        });
                        self.data.fail_upload(&id, true, detail);
                        self.note = "Queue paused. Click Start Queue to resume.".into();
                        self.save();
                    }
                    Action::Prepare if job.phase == Phase::Preparing => {
                        job.phase = Phase::PrepareFailed;
                        job.detail = result
                            .err()
                            .unwrap_or_else(|| "Snapshot was not confirmed.".into());
                        self.save();
                    }
                    Action::Status if job.phase.submitted() => {
                        if let Err(error) = result {
                            job.phase = Phase::StatusFailed;
                            job.detail = error;
                            self.data.started = false;
                            self.note = "Queue paused after losing worker status. Click Start Queue to resume uploads.".into();
                            self.save();
                        }
                    }
                    _ => {}
                }
            }
        }
    }

    fn publish(&mut self, app: &App) {
        let gpu = app.global::<CloudGpu>();
        gpu.set_queue_available(self.storage_ok);
        gpu.set_queue_started(self.data.started);
        gpu.set_queue_note(self.note.as_str().into());
        let rows = self
            .data
            .jobs
            .iter()
            .enumerate()
            .map(|(index, job)| GpuQueueRow {
                id: job.id.as_str().into(),
                title: job.label.as_str().into(),
                state: job.phase.label().into(),
                detail: job.detail.as_str().into(),
                output: job.output.as_str().into(),
                progress: job.progress,
                retry: job.phase.can_retry(),
                cancel: self.can_cancel(index),
                delete: self.can_delete(index),
                up: self.can_move(index) && (0..index).any(|i| self.can_move(i)),
                down: self.can_move(index)
                    && (index + 1..self.data.jobs.len()).any(|i| self.can_move(i)),
            })
            .collect::<Vec<_>>();
        gpu.set_jobs(ModelRc::new(VecModel::from(rows)));
        self.changed = false;
    }
}

pub(super) fn wire(app: &App) -> slint::Timer {
    let timer = slint::Timer::default();
    let gpu = app.global::<CloudGpu>();
    let controller = match Controller::open() {
        Ok(controller) => Rc::new(RefCell::new(controller)),
        Err(error) => {
            gpu.set_queue_note(format!("GPU Queue is unavailable: {error}").into());
            return timer;
        }
    };
    controller.borrow_mut().publish(app);
    let handle = Rc::clone(&controller);
    gpu.on_queue_add(move |path| handle.borrow_mut().add(path.as_str()));
    let handle = Rc::clone(&controller);
    gpu.on_queue_action(move |action, id| {
        handle.borrow_mut().control(action.as_str(), id.as_str())
    });
    let weak = app.as_weak();
    timer.start(
        slint::TimerMode::Repeated,
        Duration::from_millis(250),
        move || {
            if let Some(app) = weak.upgrade() {
                controller.borrow_mut().tick(&app);
            }
        },
    );
    timer
}
