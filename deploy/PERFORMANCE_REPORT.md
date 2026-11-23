# Performance report (Concat Web only)

GPU Config → choose Last hour, Last 24 hours or Last 7 days → Download Report
(.txt). This authenticated GET exports only the signed-in owner's data; no
external API call is made by download. Report work runs off the ASGI event loop
and the response is sent in chunks with private/no-store headers.

## Collection and available data

A dedicated background thread samples local job activity every 10 seconds while
work is pending or the worker is enabled. It polls the saved Salad group and
existing worker concurrency API at most every 20 seconds, separately from the
production dispatcher. Worker API reads require a recent running/ready Salad
instance observation and are blocked by the stop journal. An owner-specific file
lock prevents duplicate collectors
across web processes. Closing the page does not stop collection. No lifecycle
mutations are made by telemetry.

Data is in the owner's existing cloud_render.sqlite3 database:

- metric_samples: timestamped allowlisted readings, state transitions,
  collector/restart markers and image-pull decisions.
- metric_jobs: latest state/metrics used to track changes and retries.

History survives successful media auto-deletion and web restarts. The retained
history is bounded to 7 days OR the latest 20,000 samples, whichever is shorter;
actual available timestamps are included in the report. Existing historical
progress cannot be recovered before collection was installed.

Only supplied numeric readings are recorded: Salad CPU/RAM and any supported
GPU/VRAM fields; worker active_renders/concurrency; and worker job frame counts,
render duration, video duration, average_fps and speed_multiplier. The current
worker supplies FPS/speed summaries after rendering; these are labelled summary
metrics and are not presented as live per-job FPS. Missing fields are omitted.
No log parsing or GPU image changes are included.

If verified push delivery beats status polling, the background collector attempts a
bounded read of that exact worker job's final summary, verifying its saved hash
and render mode. Summary recovery is retried at most every 20 seconds for five minutes after
completion; no dispatcher network wait is added. A failed summary request never
prevents verified completion.

Raw API bodies, exception messages, credentials, private URLs and tokens are never copied
into samples or exported. Salad API collection failures now retain only a fixed
failure category and, when available, the numeric HTTP status (for example,
429 or 503). The report groups succeeded and failed polls by these safe codes;
failed polls never count as zero resource utilization. Job attribution uses owner-local job IDs. Image-pull
reallocation candidates include list/final raw progress, normalized percentage,
threshold, timer origin, decision and instance identity.

## Interpretation

All TXT timestamps use UTC. Resource timestamps are receipt times; Salad does
not provide a separate sample timestamp for these readings. Resource means are
sample means, not time-weighted measurements. Completed-render average_fps and
speed_multiplier are worker-reported, with frame/video/render durations where
available. The export also calculates effective rendering FPS when a completed
worker summary provides a positive rendered/total frame count and positive
render_time_seconds. It labels this figure as calculated; it is not a live FPS
sample, and no frame rate is inferred from queue start/end times alone.
Where completed job summaries can be attributed to measured concurrency slots,
the report includes aggregate effective FPS = sum(worker frames) / sum(worker
render seconds). This is a duration-weighted per-job speed, not total GPU
throughput, and is not extrapolated to unmeasured work. Job observations remain separate for parallel jobs.

State durations measure observed transitions. First observations can be partway
through a stage; the report cannot reconstruct time before installation, a
retention cutoff or service downtime. Restart markers break duration continuity.
The most recent open state interval is not extrapolated to download time.

Concurrency comparisons show measured active wall-time coverage, completion
counts and observed jobs/hour by configured concurrency. Gaps over 35 seconds,
instance switches and changes of concurrency are excluded from coverage. Both
settings must have measured coverage before comparison is possible. Jobs can
differ in duration and workload: this is observational data, not a controlled
benchmark or an automatic tuning recommendation. Parallel job durations are
never added and labelled total wall time.

## Checks

Offline tests cover safe Salad failure categories/HTTP status, calculated FPS,
parallel attribution, invalid/missing numbers, retry state,
push-delivery summaries, restart and deletion persistence, bounded history,
resource polling limits, secret exclusion, observed wall-time accounting,
authentication, revoked access, owner isolation, TXT response and time ranges.
The DOM harness checks export controls. A real Chromium responsive/download
check passes in CI; local launch was blocked by executor socket restrictions.
No live GPU acceptance or VPS deployment is claimed.
