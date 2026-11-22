# SaladCloud Auto Worker (Concat Web only)

GPU Config has a **Salad Connection Setup** section that opens when incomplete
and collapses after configuration. Save the organization/project/container-group
slugs there and reuse the existing Salad API key, gateway URL and renderer token.
The visible **Salad Worker Automation** section has Automatic/Manual mode,
threshold/timer inputs with +/− controls, a dynamic rule summary, 1-minute idle
shutdown, Save Automation Settings, and manual Start Worker / Stop Worker
controls. Blank slugs fall back to
the existing `CONCAT_SALAD_*` environment identifiers. This is opt-in; existing
installations retain manual start unless enabled.

Workflow: Drive ZIP download → verify all pending ZIPs → start Salad group →
check image pull → wait for API readiness and worker health → render → verified
MP4 delivery → empty queue and no transfers → stop after **60 idle seconds**.
Drive intake continues while Production is paused; Pause/Disconnect block new
GPU starts/submissions. Active jobs continue through the existing recovery path.
All pending Drive intake, including scheduled Drive retries, blocks new dispatch.
Permanent failed/cancelled entries are terminal and do not block other jobs.
The existing five Drive retries at 20-second intervals are unchanged.

## Settings

| Setting | Default | Allowed range |
| --- | --- | --- |
| Minimum image pull | 8% | 1–100%, whole numbers |
| Download window | 20 seconds | 5–600 seconds |
| Maximum reallocations | 3 | Hard cap 3 (backend can lower to 0–3) |
| Reallocation cooldown | 30 seconds | 5–600 seconds |

The UI adjusts threshold/timer. The backend persists a maximum of three
reallocations and a 30-second cooldown; API validation forbids a budget above three.
Timer origin is the instance's `update_time` when its state is `downloading`.
`pulling_progress` is a percentage from 0–100. At or above the threshold the
current download is kept. Below the threshold after the window, request
reallocation of that instance. Missing/invalid progress or timestamps wait for
valid API data; they are never interpreted as zero. Time spent allocating or
creating does not count. A changed instance/machine/version or new download
transition gets its own measurement window. No machine history is retained.

List responses are checked against HTTP Age/Date when supplied. Immediately
before reallocation, GET the exact instance again and confirm its ID, machine,
version, downloading state, transition timestamp, readiness and progress. Changed
or uncertain information blocks the mutation. Regressing state timestamps are
rejected; completed/ready instances are never reallocated.

The API is polled at most every three seconds during startup (with a second
confirmation GET only when considering reallocation). Decisions occur
on the first valid poll at/after the deadline, so network/API delays can defer
the check. The API does not provide historical samples for an exact past second.
Existing render concurrency (1 or 2 jobs on the same GPU) is preserved.

## Durable request handling

The dispatcher holds its existing cross-process file lock and the owner manager
lock around lifecycle decisions. Start/reallocation intent is committed to the
owner database before POST, and remains across process restarts. Reallocation
attempts/cooldown persist; the budget resets after confirmed worker readiness or
an explicit retry. Active/submitted jobs never trigger automatic reallocation.

Accepted or timed-out POSTs are reconciled by GET. The same stopped group or
instance/download transition never receives another automatic identical POST.
If confirmation never arrives (including a rejected mutation), inspect Salad,
then use **Retry Worker Action** to clear the journals/budget and pending manual
command. This requires login and CSRF and is blocked while GPU jobs are active. Retry is an explicit request
to reconcile/retry; do not repeatedly click it while Salad is changing state.

## Dedicated group and access

Use one replica dedicated to Concat Web, with Salad `autostart_policy=false`.
The management API key needs GET group/instances, START, REALLOCATE and STOP
permissions. A different replica count or enabled/unknown Salad autostart blocks
automatic startup. Worker health must succeed before dispatch. Identifiers and
settings are owner scoped; API/worker keys are never returned to the browser.
Requests use the fixed HTTPS Salad API origin and do not follow redirects.

Automatic mode includes 60-second shutdown. Saving Manual mode disables
legacy automatic shutdown as well. Legacy API fields/saved timeouts remain
compatible until an explicit mode save; they are no longer shown in the UI.
Manual controls enqueue commands into the same dispatcher, never a second
controller. Manual Start respects the Drive gate and waits for readiness; it
does not automatically reallocate slow instances. Manual Stop pauses new
submissions and waits for active renders/transfers. Repeated commands use the
same durable journals. A lost stop acknowledgement is never automatically
repeated against a still-running group. Shutdown also checks pending output
transfers and `active_renders=0` on the worker. Unreachable/busy workers or API
uncertainty block shutdown. Failed post-submission jobs with unverified output
also block shutdown until retried or explicitly resolved, preserving GPU output. A web restart begins a fresh idle window.

## Verification and release

The requested partial branch `feature/salad-auto-worker-lifecycle-20261122`
was not present in remote branches or PR search during inspection on 2026-11-22.
This implementation starts cleanly from canonical main `c5e9fb459e7ff82e82298c83d3d13a02779196ca`.
No project-image extraction health-check logic was adopted.

Offline coverage: complete two-ZIP intake/start/readiness/render/push-delivery/
60-second stop workflow; pending Drive barrier; exact threshold; timestamp
origin; missing progress; bounded retries/cooldown; durable ambiguous requests;
active-render protection; cancellation; owner isolation; CSRF; config validation.
Run `python -m unittest tests.test_salad_auto_worker app.modules.concat.test_routes -v`
plus the essential/security regression suite. CI runs these tests without
contacting live Salad/Drive or deploying anything.

Only Concat Web changes. Do not merge the prior GPU image-progress branch,
change the `concat` GPU worker, rebuild its Docker image, select preferred
machines, or track machine performance. Deploy only a reviewed, CI-verified
exact `main` SHA, after separate live VPS approval. External API/Drive acceptance
remains a staging check; offline tests do not claim a live GPU run.
The API exposes state-transition time but no separate progress-sample timestamp
or atomic machine/state condition for reallocation. Fresh HTTP metadata,
monotonic transition checks and immediate instance re-fetch reduce stale-data
risk; undetectably stale upstream samples and a remote transition after the last
GET cannot be proven absent. Unconfirmed mutations require explicit recovery.
Full browser rendering was unavailable in this executor; the real UI script is
covered by the offline DOM harness and template/API tests.

API references:

- https://docs.salad.com/reference/saladcloud-api/container-groups/get-container-group-instance
- https://docs.salad.com/reference/saladcloud-api/container-groups/list-container-group-instances
- https://docs.salad.com/reference/saladcloud-api/container-groups/reallocate-container-group-instance
- https://docs.salad.com/reference/saladcloud-api/container-groups/start-container-group
- https://docs.salad.com/reference/saladcloud-api/container-groups/stop-container-group
