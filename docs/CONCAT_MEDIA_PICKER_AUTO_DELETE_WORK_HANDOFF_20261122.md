# Concat Web — Media Picker, Queue Deduplication & Verified Auto-Delete

**Date:** 2026-11-22  
**Repository:** `dilshadshahsyed883-cmd/concat-web`  
**Feature branch:** `feature/concat-media-picker-upload-cleanup-20261122`  
**Base:** `main` (do not modify or merge main without separate user approval)  
**Status:** Implementation **in progress** on the feature branch; **not tested or production deployed**.

## 1. User-approved scope

The **Concat Studio > Uploader > Upload Engine** page presently exposes a manual **Concat Video Link** input. Add a **Concat Media** dropdown beside it and automate local media cleanup when YouTube upload is independently verified.

1. Keep the **manual Concat Video Link** input functional.
2. Add a **Concat Media** video dropdown beside it (desktop); stack responsively on mobile.
3. Show **only verified, completed, locally available MP4s belonging to the authenticated current owner**.
4. **Hide** a video as soon as it is queued or scheduled, and during all pending/active upload states.
5. **Hide** already successfully uploaded videos, regardless of whether the queue entry was archived.
6. Retryable **failed or cancelled** items may be selectable again **only if no YouTube-side video was created/verified and safe retry/recovery cannot duplicate the upload**. If a remote video ID/verification exists, remain hidden.
7. Selecting a video fills the manual URL input automatically. Refresh must fetch current availability.
8. Add **server-side, race-safe, owner-wide deduplication**: the same Concat video must not be queued twice via dropdown or manual link, even for different YouTube channels or concurrent browser requests.
9. On **independently verified successful YouTube upload**, **delete the original MP4 from that owner's Concat Media local VPS storage**, not merely the uploader's staged copy. Leave YouTube video, queue history, YouTube ID/URL, channel metadata and logs intact.
10. Never delete original Concat Media on pending, scheduled, uploading, unverified, failed, ambiguous or cancelled jobs.
11. If verified cleanup fails, persist the verified receipt and retry **cleanup only**; **never re-upload** to YouTube because of a cleanup error.
12. If any other active queue job still needs that same source, **defer deletion** and perform eventual cleanup when safe.
13. Do **not** delete Google Drive originals or copies, change render pipeline or GPU code, or alter the rest of Uploader / Sheets / Channels / Queue functionality. Preserve tenant isolation and existing access controls.

## 2. UX / event flow

```
Concat Media: completed + owned + verified + physically present
    -> filter queued, scheduled, uploading, uploaded / reserved sources
    -> choose video from dropdown
    -> auto-fill Concat Video Link (manual paste still possible)
    -> select group/channel and metadata
    -> Add Video to Queue (server rejects duplicates atomically)
    -> queued/scheduled video disappears from available dropdown
    -> independent YouTube upload and remote verification
    -> verified receipt saved
    -> staged copy cleanup
    -> original Concat Media MP4 cleanup after safety checks
    -> queue history stays, dropdown remains filtered
```

**State rules**

| State | Dropdown | Delete original |
| --- | --- | --- |
| Render complete; verified owner MP4; not claimed | Show | No |
| Queued, scheduled, waiting, claimed, uploading | Hide | No |
| YouTube remotely verified / cleanup pending | Hide | Yes, only when safe |
| Completed / uploaded | Hide | Should already be removed; retry if needed |
| Failed / cancelled, definitely no remote video | Show if source intact and safe | No |
| Failed / ambiguous with remote ID, verified receipt or possible duplicate | Hide pending safe recovery | No |
| Concat file busy transferring to Drive | Hide unless ready | No |

## 3. Existing architecture traced on 2026-11-22

- Concat UI iframe: `app/modules/uploader/templates/workspace.html` loads `/concat-uploader/app?tab=add_video`.
- Uploader form: `reused/uploader/part01/templates/app.html` (Concat-specific source section).
- Page and add-video action: `reused/uploader/part01/lum_engine/standalone_app.py`.
- Concat link parser / verified local copy: `reused/uploader/part01/lum_engine/concat_source.py`.
- Queue DB & owner handling: `reused/uploader/part01/lum_engine/project_store.py`.
- Upload worker, YouTube verification and staged cleanup: `reused/uploader/part01/lum_engine/worker.py`.
- Owner-specific canonical media DB/file: `app/modules/concat/engine/cloud_render.py`, under `CONCAT_CLOUD_RENDER_ROOT/owners/<sha256(username)>/{cloud_render.sqlite3,media/<job>.mp4}`.
- Owner auth / gateway allowlist: `app/modules/uploader/gateway.py`.
- Existing owner route: `GET /api/cloud-render/media`, `GET /api/cloud-render/media/{job}/file`, `DELETE /api/cloud-render/media/{job}` in `app/modules/concat/routes.py`.
- Current source handoff is a **verified private copy**; deleting that staged copy does *not* remove Concat Media original.
- Existing `uq_copyright_source_channel` fingerprint index prevents some duplicates **per channel**, not reliably across all channels.

## 4. Code already written on the separate branch (review and finish)

**These changes have been committed but are not yet tested or signed off:**

1. `project_store.py`: new `concat_job_id` column, owner/source reservation helpers and atomic SQLite trigger for cross-channel duplicate admission.
2. `concat_source.py`: owner-safe media listing and checksum-verified deletion helper for canonical Concat Media.
3. `standalone_app.py`: new owner-scoped `GET /concat-uploader/api/concat/available-media`, selection list based on stored reservations, and explicit `concat_job_id` on queue admission.
4. `worker.py`: cleanup invocation after independent YouTube verification, deferring when another active project depends on the original.
5. `app/modules/uploader/gateway.py`: allowlist the new authenticated GET API.
6. `templates/app.html`: Concat Media dropdown beside link field.
7. `static/concat_media_picker.js`: refresh, dropdown population, auto-fill URL, and manual-link coexistence.
8. `static/copyright_workspace.css`: responsive desktop/mobile picker layout.
9. `templates/base.html`: load picker JavaScript for Concat instance only.

**Important:** No full end-to-end validation, migration verification, deployment, PR merge or production rollout has been completed. The feature is a draft, not a confirmed working release.

## 5. Required completion and review tasks for Work mode

1. **Inspect feature branch versus current main**, read the entire relevant module paths, and check all changes for correctness/security. Do not assume the draft code is perfect.
2. Ensure `concat_job_id` schema migration and trigger are safe on existing persistent SQLite, including multiple historical records, upgrades and concurrent insertions. Avoid breaking Copyright/other uploaders.
3. Verify filtering across `QUEUED`, `WAITING_SLOT`, `SCHEDULED`, `UPLOADING`, `VERIFYING_REMOTE`, `CLEANING`, `FAILED`, `PAUSED`, `CANCELLED`, `COMPLETED`, **archived history**, and YouTube remote-ID/receipt corner cases.
4. Verify that manual Concat URLs and dropdown selections invoke identical **authoritative server-side** duplicate checks; different channel selection must not bypass checks.
5. Review whether existing historical uploads without `concat_job_id` can still duplicate; use matching verified fingerprints/receipts as applicable while never inferring an unsafe filesystem path from a title.
6. Verify original cleanup is idempotent and refuses symlinks, path traversal, cross-owner paths, checksum/size changes, and active Drive transfers; make the DB and filesystem cleanup crash-safe or recoverable.
7. **Fix deferred-cleanup eventuality:** if original deletion is deferred because another active job uses it, it must later be retried when no active users remain; don't mark cleanup fully complete without an eventual cleanup path.
8. Check the post-verification `_finish_verified` route after worker crash or cleanup error. Ensure it cannot create a second YouTube upload, even after restart.
9. Check `delete_staged_after_verified` setting interactions: user wants canonical Concat original auto-deletion **after verified success**, while never deleting on unverified/failed results.
10. Confirm `CONCAT_PUBLIC_ORIGIN` consistency, authenticated same-origin API usage, no unsafe outbound URL fetches, owner-scoped storage, per-instance uploader data, and gateway/SSO restrictions.
11. Confirm dropdown refresh on navigation/history/queue changes, empty state, API failure state, name escaping and **manual URL still usable**. Preserve existing selected title, metadata, group/channel and schedule fields.
12. Verify responsive two-column layout above mobile breakpoint and one-column at mobile width; no overlap or clipped controls.
13. Run targeted Python tests and any available UI, static, lint, release security, and deployment-preflight tests. Add regression tests in `tests/` for owner isolation, all states, duplicates, race, cleanup gating, retries, failure and legacy state.
14. Document deployment/rollback and key operational logs, and ensure code compiles and tests pass.
15. Commit final fixes **only to the feature branch**; open a **draft PR to main** with test results and remaining risks for explicit human approval. **Do not merge, deploy, restart services, delete production media or modify live production data.**

## 6. Acceptance criteria

- Dropdown contains only owner's available completed MP4s, no queued/scheduled/uploaded.
- Manual link works and is protected by same duplicate checks.
- Concurrent attempts across two channels produce at most **one** queued upload.
- Failed/unverified jobs retain original; ambiguous remote cases never silently re-submit.
- Independently YouTube-verified success eventually deletes the exact Concat Media original while retaining upload history and verified receipt.
- Cleanup failure can safely retry without another YouTube upload.
- No cross-tenant disclosure, arbitrary file deletion, Drive original deletion or regression in existing uploader tabs.
- CI/tests pass; no main merge or production deploy before explicit approval.

## 7. Work mode continuation instructions

Use the GitHub repository and exact feature branch above. Compare with main, inspect/repair already committed code, add tests, execute verification, and provide branch/commit/PR links. Keep all work in this feature branch; do not create another branch unless necessary and explained. If a live secret or privileged production operation would be needed, skip it and report the blocker. If a test cannot be run, report exactly which test and why. Never report 'completed' until the acceptance criteria are met.
