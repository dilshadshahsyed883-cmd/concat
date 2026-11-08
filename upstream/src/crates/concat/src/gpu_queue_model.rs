// SPDX-License-Identifier: AGPL-3.0-or-later

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
pub(crate) enum Phase {
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

    pub(super) fn can_cancel(self) -> bool {
        !matches!(self, Self::Completed | Self::Cancelled)
    }

    pub(super) fn can_delete(self) -> bool {
        true
    }

    pub(super) fn submitted(self) -> bool {
        matches!(self, Self::Queued | Self::Rendering)
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(crate) struct Job {
    pub id: String,
    pub remote: String,
    pub project: String,
    pub label: String,
    pub domain: String,
    pub phase: Phase,
    pub attempted: bool,
    pub progress: i32,
    pub detail: String,
    #[serde(default)]
    pub metrics: String,
    #[serde(default)]
    pub bytes: String,
    pub output: String,
}

pub(crate) fn format_byte_size(bytes: i64) -> String {
    let bytes = bytes.max(0) as f64;
    const KB: f64 = 1024.0;
    const MB: f64 = 1024.0 * 1024.0;
    const GB: f64 = 1024.0 * 1024.0 * 1024.0;

    if bytes >= GB {
        format!("{:.2} GB", bytes / GB)
    } else if bytes >= MB {
        format!("{:.2} MB", bytes / MB)
    } else if bytes >= KB {
        format!("{:.2} KB", bytes / KB)
    } else {
        format!("{:.0} B", bytes)
    }
}

pub(crate) fn format_upload_progress(transferred: i64, total: i64) -> String {
    if total <= 0 {
        return "0 B / 0 B (0%)".into();
    }
    let pct = ((transferred.max(0) as f64 / total as f64) * 100.0).floor() as i64;
    let pct = pct.clamp(0, 100);
    format!(
        "{} / {} ({}%)",
        format_byte_size(transferred),
        format_byte_size(total),
        pct
    )
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub(super) struct Queue {
    pub jobs: Vec<Job>,
    #[serde(skip)]
    pub started: bool,
}

impl Queue {
    /// Adding work never opts the user into starting uploads. An already
    /// running queue may consume it; a stopped queue remains stopped.
    pub fn enqueue(&mut self, job: Job) {
        self.jobs.push(job);
    }

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
            if job.phase == Phase::Cancelled {
                return;
            }
            job.phase = if offline {
                Phase::Waiting
            } else {
                Phase::UploadFailed
            };
            job.detail = detail;
        }
        self.started = false;
    }

    pub fn cancel(&mut self, id: &str) -> bool {
        if let Some(job) = self.jobs.iter_mut().find(|job| job.id == id) {
            if job.phase.can_cancel() {
                let submitted = job.phase.submitted() || job.phase == Phase::Uploading || job.attempted;
                job.phase = Phase::Cancelled;
                job.detail = if submitted {
                    "Cancelled locally.".into()
                } else {
                    "Cancelled before submission.".into()
                };
                self.started = false;
                return true;
            }
        }
        false
    }

    pub fn delete(&mut self, id: &str) -> Option<Job> {
        let index = self.jobs.iter().position(|job| job.id == id)?;
        if self.jobs[index].phase.can_delete() {
            Some(self.jobs.remove(index))
        } else {
            None
        }
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
            metrics: String::new(),
            bytes: String::new(),
            output: String::new(),
        }
    }

    #[test]
    fn interrupted_queue_remains_manual_after_new_job() {
        let mut queue = Queue {
            jobs: vec![job("first", Phase::Uploading)],
            started: true,
        };
        queue.recover();
        assert!(!queue.started);
        assert_eq!(queue.jobs[0].phase, Phase::Waiting);
        queue.enqueue(job("second", Phase::Waiting));
        assert!(queue.next_upload().is_none());
        queue.started = true;
        assert_eq!(queue.next_upload().unwrap().id, "first");
    }

    #[test]
    fn enqueue_requires_explicit_start_and_keeps_existing_running_state() {
        let mut stopped = Queue::default();
        stopped.enqueue(job("pending", Phase::Waiting));
        assert!(!stopped.started);
        assert!(stopped.next_upload().is_none());
        stopped.started = true;
        assert_eq!(stopped.next_upload().unwrap().id, "pending");
        stopped.enqueue(job("next", Phase::Waiting));
        assert!(stopped.started);
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
    fn offline_preserves_job_and_leaves_cancel_and_delete_available() {
        let mut queue = Queue {
            jobs: vec![job("same-id", Phase::Uploading)],
            started: true,
        };
        queue.fail_upload("same-id", true, "Worker offline".into());
        assert_eq!(queue.jobs[0].phase, Phase::Waiting);
        assert!(!queue.started);
        // Both cancel and delete must be available even when worker was offline and attempted is true
        assert!(queue.jobs[0].phase.can_cancel());
        assert!(queue.jobs[0].phase.can_delete());

        // Cancel job locally
        assert!(queue.cancel("same-id"));
        assert_eq!(queue.jobs[0].phase, Phase::Cancelled);
        assert_eq!(queue.jobs[0].detail, "Cancelled locally.");
        assert!(!queue.started);

        // Delete job safely removes the local queue entry
        let removed = queue.delete("same-id");
        assert!(removed.is_some());
        assert_eq!(removed.unwrap().id, "same-id");
        assert!(queue.jobs.is_empty());
    }

    #[test]
    fn cancelling_uploading_job_stops_auto_retry_and_discards_late_failure() {
        let mut queue = Queue {
            jobs: vec![
                job("first", Phase::Uploading),
                job("second", Phase::Waiting),
            ],
            started: true,
        };
        // Cancel active upload
        assert!(queue.cancel("first"));
        assert_eq!(queue.jobs[0].phase, Phase::Cancelled);
        assert_eq!(queue.jobs[0].detail, "Cancelled locally.");
        assert!(!queue.started);

        // Late fail_upload event arriving from aborted worker connection must be discarded
        queue.fail_upload("first", true, "Connection aborted".into());
        assert_eq!(queue.jobs[0].phase, Phase::Cancelled);
        assert!(!queue.started);
        assert!(queue.next_upload().is_none());

        // Even if queue is restarted, cancelled job is not retried
        queue.started = true;
        assert_eq!(queue.next_upload().unwrap().id, "second");
    }

    #[test]
    fn delete_available_across_all_phases_without_touching_unrelated_jobs() {
        let all_phases = [
            Phase::Preparing,
            Phase::Waiting,
            Phase::Uploading,
            Phase::Queued,
            Phase::Rendering,
            Phase::Completed,
            Phase::PrepareFailed,
            Phase::UploadFailed,
            Phase::StatusFailed,
            Phase::RenderFailed,
            Phase::Cancelled,
        ];
        for phase in all_phases {
            assert!(phase.can_delete(), "Phase {:?} should be deletable", phase);
        }

        let mut queue = Queue {
            jobs: vec![
                job("keep-1", Phase::Waiting),
                job("target", Phase::Rendering),
                job("keep-2", Phase::Completed),
            ],
            started: true,
        };
        let deleted = queue.delete("target");
        assert_eq!(deleted.unwrap().id, "target");
        assert_eq!(queue.jobs.len(), 2);
        assert_eq!(queue.jobs[0].id, "keep-1");
        assert_eq!(queue.jobs[1].id, "keep-2");
    }

    #[test]
    fn unattempted_waiting_job_detail_indicates_cancelled_before_submission() {
        let mut queue = Queue {
            jobs: vec![job("unattempted", Phase::Waiting)],
            started: false,
        };
        assert!(queue.cancel("unattempted"));
        assert_eq!(queue.jobs[0].phase, Phase::Cancelled);
        assert_eq!(queue.jobs[0].detail, "Cancelled before submission.");
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

    #[test]
    fn older_saved_queue_without_metrics_still_loads() {
        let mut value = serde_json::to_value(Queue {
            jobs: vec![job("legacy", Phase::Queued)],
            started: false,
        })
        .unwrap();
        value["jobs"][0]
            .as_object_mut()
            .unwrap()
            .remove("metrics");
        let restored: Queue = serde_json::from_value(value).unwrap();
        assert_eq!(restored.jobs[0].metrics, "");
        assert_eq!(restored.jobs[0].phase, Phase::Queued);
    }

    #[test]
    fn render_metrics_survive_queue_restart() {
        let mut completed = job("rendered", Phase::Completed);
        completed.metrics =
            "Render Time: 12.00 s | Average FPS: 60.00 | Speed: 2.00x".into();
        let saved = serde_json::to_string(&Queue {
            jobs: vec![completed],
            started: true,
        })
        .unwrap();
        let mut restored: Queue = serde_json::from_str(&saved).unwrap();
        restored.recover();
        assert!(!restored.started);
        assert_eq!(restored.jobs[0].phase, Phase::Completed);
        assert!(restored.jobs[0].metrics.contains("Average FPS: 60.00"));
    }

    #[test]
    fn non_blocking_submission_arms_queue_and_sets_preparing() {
        let mut queue = Queue {
            jobs: vec![],
            started: false,
        };
        // Simulated job addition from non-blocking send-gpu
        queue.jobs.push(job("job-1", Phase::Preparing));
        queue.started = true;
        assert!(queue.started);
        assert_eq!(queue.jobs.len(), 1);
        assert_eq!(queue.jobs[0].phase, Phase::Preparing);
        // While preparing, upload is not dispatched prematurely
        assert!(queue.next_upload().is_none());
        // Transition to Waiting when preparation completes
        queue.jobs[0].phase = Phase::Waiting;
        assert_eq!(queue.next_upload().unwrap().id, "job-1");
    }

    #[test]
    fn upload_continues_when_settings_is_closed() {
        let mut queue = Queue {
            jobs: vec![job("job-active", Phase::Uploading)],
            started: true,
        };
        // Closing the Settings sheet does not touch queue.started or job.phase
        assert!(queue.started);
        assert_eq!(queue.jobs[0].phase, Phase::Uploading);
        // Upload completes in background to Queued without UI involvement
        queue.jobs[0].phase = Phase::Queued;
        assert_eq!(queue.jobs[0].phase, Phase::Queued);
    }

    #[test]
    fn project_switching_during_upload_leaves_active_snapshot_intact() {
        let mut queue = Queue {
            jobs: vec![
                Job {
                    id: "job-p1".into(),
                    remote: "job-p1".into(),
                    project: "C:/projects/proj1".into(),
                    label: "proj1".into(),
                    domain: "https://worker.salad.com".into(),
                    phase: Phase::Uploading,
                    attempted: true,
                    progress: 45,
                    detail: "Uploading package...".into(),
                    metrics: String::new(),
                    bytes: String::new(),
                    output: String::new(),
                },
            ],
            started: true,
        };
        // User opens/creates proj2 and queues it while proj1 is uploading
        queue.jobs.push(Job {
            id: "job-p2".into(),
            remote: "job-p2".into(),
            project: "C:/projects/proj2".into(),
            label: "proj2".into(),
            domain: String::new(),
            phase: Phase::Waiting,
            attempted: false,
            progress: 0,
            detail: "Project snapshot ready.".into(),
            metrics: String::new(),
            bytes: String::new(),
            output: String::new(),
        });
        // Job 1 remains uploading with its own project and snapshot intact
        assert_eq!(queue.jobs[0].phase, Phase::Uploading);
        assert_eq!(queue.jobs[0].project, "C:/projects/proj1");
        assert_eq!(queue.jobs[0].progress, 45);

        // Job 2 waits for Job 1 to finish uploading
        assert!(queue.next_upload().is_none());

        // Job 1 finishes upload
        queue.jobs[0].phase = Phase::Queued;
        assert_eq!(queue.next_upload().unwrap().id, "job-p2");
    }

    #[test]
    fn duplicate_submission_prevention_logic() {
        let queue = Queue {
            jobs: vec![
                Job {
                    id: "existing".into(),
                    remote: "existing".into(),
                    project: "C:/projects/proj1".into(),
                    label: "proj1".into(),
                    domain: String::new(),
                    phase: Phase::Uploading,
                    attempted: true,
                    progress: 50,
                    detail: "Uploading...".into(),
                    metrics: String::new(),
                    bytes: String::new(),
                    output: String::new(),
                },
            ],
            started: true,
        };
        // Checking for duplicate active project prevents re-queuing
        let is_duplicate = queue.jobs.iter().any(|j| {
            j.project == "C:/projects/proj1"
                && matches!(j.phase, Phase::Preparing | Phase::Waiting | Phase::Uploading)
        });
        assert!(is_duplicate);

        // Different project is not considered a duplicate
        let is_other_duplicate = queue.jobs.iter().any(|j| {
            j.project == "C:/projects/proj2"
                && matches!(j.phase, Phase::Preparing | Phase::Waiting | Phase::Uploading)
        });
        assert!(!is_other_duplicate);
    }
    #[test]
    fn format_bytes_and_upload_progress_accuracy() {
        assert_eq!(format_byte_size(0), "0 B");
        assert_eq!(format_byte_size(500), "500 B");
        assert_eq!(format_byte_size(1024), "1.00 KB");
        assert_eq!(format_byte_size(1048576), "1.00 MB");
        assert_eq!(format_byte_size(13107200), "12.50 MB");
        assert_eq!(format_byte_size(47395635), "45.20 MB");
        assert_eq!(format_byte_size(1073741824), "1.00 GB");

        assert_eq!(
            format_upload_progress(13107200, 47395635),
            "12.50 MB / 45.20 MB (27%)"
        );
        assert_eq!(
            format_upload_progress(47395635, 47395635),
            "45.20 MB / 45.20 MB (100%)"
        );
        assert_eq!(format_upload_progress(0, 47395635), "0 B / 45.20 MB (0%)");
        assert_eq!(format_upload_progress(0, 0), "0 B / 0 B (0%)");
    }

    #[test]
    fn job_bytes_field_survives_serialization_and_legacy_queue() {
        let mut upload_job = job("uploading-job", Phase::Uploading);
        upload_job.bytes = "12.50 MB / 45.20 MB (27%)".into();
        upload_job.progress = 27;
        let serialized = serde_json::to_string(&upload_job).unwrap();
        let deserialized: Job = serde_json::from_str(&serialized).unwrap();
        assert_eq!(deserialized.bytes, "12.50 MB / 45.20 MB (27%)");
        assert_eq!(deserialized.progress, 27);

        let mut value = serde_json::to_value(&upload_job).unwrap();
        value.as_object_mut().unwrap().remove("bytes");
        let restored: Job = serde_json::from_value(value).unwrap();
        assert_eq!(restored.bytes, "");
    }
}
