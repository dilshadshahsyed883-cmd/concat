// SPDX-License-Identifier: AGPL-3.0-or-later

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
pub(super) enum Phase {
    Preparing,
    Waiting,
    Uploading,
    Queued,
    Rendering,
    Completed,
    PrepareFailed,
    UploadFailed,
    StatusFailed,
    RenderFailed,
    Cancelled,
}

impl Phase {
    pub(super) fn label(self) -> &'static str {
        match self {
            Self::Preparing => "Preparing snapshot",
            Self::Waiting => "Waiting for Worker",
            Self::Uploading => "Uploading",
            Self::Queued => "Uploaded / Worker Queue",
            Self::Rendering => "GPU Rendering",
            Self::Completed => "Completed",
            Self::PrepareFailed | Self::UploadFailed | Self::StatusFailed | Self::RenderFailed => {
                "Error"
            }
            Self::Cancelled => "Cancelled",
        }
    }

    pub(super) fn can_retry(self) -> bool {
        matches!(
            self,
            Self::PrepareFailed | Self::UploadFailed | Self::StatusFailed | Self::RenderFailed
        )
    }

    pub(super) fn submitted(self) -> bool {
        matches!(self, Self::Queued | Self::Rendering)
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(super) struct Job {
    pub id: String,
    pub remote: String,
    pub project: String,
    pub label: String,
    pub domain: String,
    pub phase: Phase,
    pub attempted: bool,
    pub progress: i32,
    pub detail: String,
    pub output: String,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub(super) struct Queue {
    pub jobs: Vec<Job>,
    #[serde(skip)]
    pub started: bool,
}

impl Queue {
    pub fn recover(&mut self) {
        self.started = false;
        for job in &mut self.jobs {
            if job.phase == Phase::Uploading {
                job.phase = Phase::Waiting;
                // An interrupted uploader may have committed its last request.
                // Resume the same remote id after an explicit Start.
                job.attempted = true;
                job.detail = "Upload interrupted. Click Start Queue to resume.".into();
            }
        }
    }

    pub fn next_upload(&self) -> Option<&Job> {
        if !self.started || self.jobs.iter().any(|job| job.phase == Phase::Uploading) {
            return None;
        }
        // Do not overtake a job whose snapshot is still being prepared.
        self.jobs
            .iter()
            .find(|job| matches!(job.phase, Phase::Preparing | Phase::Waiting))
            .filter(|job| job.phase == Phase::Waiting)
    }

    pub fn fail_upload(&mut self, id: &str, offline: bool, detail: String) {
        if let Some(job) = self.jobs.iter_mut().find(|job| job.id == id) {
            job.phase = if offline {
                Phase::Waiting
            } else {
                Phase::UploadFailed
            };
            job.detail = detail;
        }
        self.started = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn job(id: &str, phase: Phase) -> Job {
        Job {
            id: id.into(),
            remote: id.into(),
            project: String::new(),
            label: String::new(),
            domain: String::new(),
            phase,
            attempted: false,
            progress: 0,
            detail: String::new(),
            output: String::new(),
        }
    }

    #[test]
    fn worker_rendering_does_not_block_next_upload() {
        let mut queue = Queue {
            jobs: vec![
                job("first", Phase::Uploading),
                job("second", Phase::Waiting),
            ],
            started: true,
        };
        assert!(queue.next_upload().is_none());
        queue.jobs[0].phase = Phase::Rendering;
        assert_eq!(queue.next_upload().unwrap().id, "second");
    }

    #[test]
    fn offline_preserves_job_and_requires_manual_start() {
        let mut queue = Queue {
            jobs: vec![job("same-id", Phase::Uploading)],
            started: true,
        };
        queue.fail_upload("same-id", true, "offline".into());
        assert_eq!(queue.jobs[0].phase, Phase::Waiting);
        assert!(queue.next_upload().is_none());
        queue.started = true;
        assert_eq!(queue.next_upload().unwrap().remote, "same-id");
    }

    #[test]
    fn persisted_queue_recovers_without_arming_it() {
        let initial = Queue {
            jobs: vec![
                job("same-id", Phase::Uploading),
                job("render", Phase::Rendering),
            ],
            started: true,
        };
        let mut reloaded: Queue =
            serde_json::from_str(&serde_json::to_string(&initial).unwrap()).unwrap();
        reloaded.recover();
        assert!(!reloaded.started);
        assert_eq!(reloaded.jobs[0].phase, Phase::Waiting);
        assert!(reloaded.jobs[0].attempted);
        assert_eq!(reloaded.jobs[1].phase, Phase::Rendering);
    }
}
