# Agent 4 — Concat Studio ownership and security audit

**Date:** 2026-11-18  
**Repository:** dilshadshahsyed883-cmd/vps1-deploy-control  
**Audit source baseline:** main at 9379c166026794994a64ffaffe4efaf3812c02ed  
**Reference:** plan/concat-studio-user-isolation-20261118 and docs/plans/CONCAT_STUDIO_STEP1_DISCOVERY_20261118.md  
**Scope:** source analysis, test design, safe temporary-fixture tests only. No runtime inspection, application authorization changes, data migration, production permission change, merge, or deployment.

## Verdict

**NOT CLEARED FOR NON-ADMIN CLOUD RENDER ACCESS.** Existing Cloud Render is admin-gated, not tenant-isolated. Its production queue, completed MP4 table, GPU settings and receiver-token-based callback path have no server-authenticated owner association. Replacing the admin check with a login check would expose other users' assets and operational controls. Keep the current admin gate until the complete owner model is implemented and cross-user negative tests succeed.

**Existing Copyright Uploader provides a useful, but conditional, reference.** Its special copyright instance uses strict owner boundaries for channel, Sheet, group and profile entities and owner-based wrapper queue/history. Generic uploader instances have an admin bypass; a new concat instance name would NOT automatically acquire Copyright's security rules. Do not mount an insecure variant under a different route.

**Deployment status:** Not inspected. All findings here refer to the source snapshot, not proof about currently running host data or routing.

## 1. Authentication / trusted identity

- Scene Builder, apps/scene-builder/part01/app.py lines 623-810: user_account resolves the active SQLite user; middleware checks an authenticated session, re-reads account activity/admin role per request and denies non-admin Cloud Render paths at lines 796-798. register_cloud_render additionally calls require_login and require_admin at apps/scene-builder/part01/cloud_render.py lines 557-573.
- Scene Builder Studio grants currently enumerate song, short, copyright and voice (app.py lines 675-715). Concat Studio needs its own authorization mapping, **not** a Copyright Studio grant masquerading as a Concat Studio route.
- production/scene_session_routes.py lines 17-66: trusted Nginx SSO subrequests forward the Scene-derived identity and replace inbound X-Auth-User/X-Is-Admin. A direct exposed backend or reverse-proxy route accepting untrusted client-supplied values would invalidate this trust boundary. Audit both canonical and legacy paths.
- Copyright Uploader, apps/copyright-uploader/part01/lum_engine/standalone_app.py lines 284-382: its suite authentication reads the upstream identity headers. The new path must be reachable only through a proxy that sets those headers from a validated Scene session and strips client-provided copies. Do not accept username/owner from request body, query, copied link or X-Auth-User directly from the public internet.
- Admin parity means an admin's ordinary Concat Studio UI and APIs are scoped to their own resources. A separate support/break-glass path, if needed later, requires additional authorization, audit and clearly named endpoints; do not implement generic admin bypass for Concat.

**Existing unrelated legacy exception:** Scene's older job_ownership/reconcile_job_ownership implementation at app.py lines 965-1035 automatically assigns untracked Scene jobs to primary admin and allows admin bypass by default. This must NOT be reused as Concat's migration or read-authorization policy.

## 2. Exact Cloud Render state and API change map

| Surface | Current source/API | Finding / required enforcement |
| --- | --- | --- |
| Persistent media | apps/scene-builder/part01/cloud_render.py lines 134-164; files(id,name,size,received,chunks,sha256,md5,state,drive_path,error,created_at) | **P0:** no owner identity. Add owner link or strict owner mapping without discarding legacy records; owner-scoped list, preview, rename, queue, delete, Drive destination and cleanup. |
| Persistent production jobs | apps/scene-builder/part01/cloud_render_production.py lines 313-337; production_jobs(id,remote_id,drive_id,...) | **P0:** no owner identity. Add input/job/output ownership and owner-aware indexes, state changes, retry, cancel, delete and recovery. |
| Drive input deduplication | cloud_render_production.py lines 369-392; production_jobs.drive_id globally UNIQUE, scan updates resource_keys | **P0:** one owner's Drive ID can collide with or update another owner's job. Deduplicate by (owner, Drive identity), never merge a foreign job's keys. |
| Production queue list & control | GET /api/cloud-render/production; POST /api/cloud-render/production/scan; POST /api/cloud-render/production/queue/{action}; POST /api/cloud-render/production/jobs/{job}/{action} | **P0:** snapshot and job mutations use unfiltered global queries (production.py lines 398-438). Make every read and transition owner-aware; distinguish owner Start/Pause from global worker capacity. |
| GPU Config / receiver token | GET/POST /api/cloud-render/config; POST /api/cloud-render/token/rotate; POST /api/cloud-render/gpu/test, connect, disconnect | **P0:** Cloud Render config table is global, including endpoint, Salad/worker keys and receiver token. Isolate owner-editable settings, mask secrets, pin in-flight job config; keep privileged infrastructure secrets/admin maintenance separate. |
| Media listing & actions | GET /api/cloud-render/media; POST /api/cloud-render/media/{job}/rename and /drive; DELETE /api/cloud-render/media/{job} | **P0:** manager.listing, rename, queue and delete lack owner predicates (cloud_render.py lines 356-360, 448-483). Enforce authenticated owner on every operation, including concurrency and deletion racing upload handoff. |
| Preview/download/Copy Link | GET /api/cloud-render/media/{job}/file (query download=1 optional) | **P0:** current route is admin-only and checks file ID/state, not owner (cloud_render.py lines 652-660). Preserve #767 same-origin URL and button, but require owner authorization before streaming or redirecting. A URL alone must convey no access. |
| Direct Salad callback | POST /api/cloud-render/ingest/{job}/chunk | **P0:** middleware bypasses normal session (app.py lines 770-772), requiring the global X-Cloud-Render-Token. cloud_render.py lines 663-672 verify token but not a job-to-owner binding. Authenticate worker AND require server-held pending job, bound owner, remote ID, checksum/size, request state, idempotency and replay checks. Unknown jobs must be rejected or use an explicitly attributed trusted admission record; never infer owner from job string. |
| Drive delivery | cloud_render.py lines 511-556; cloud_render_production.py DriveIntake/pull | Preserve canonical rclone identity, remote size/MD5 confirmation, verified MP4 checksums, path/symlink constraints and safe local cleanup. Copy Link must still resolve the authorized canonical asset when local file has already moved to Drive. |

The current #767 Copy Link implementation builds an absolute same-host link to the existing /file endpoint in apps/scene-builder/part01/static/cloud_render.js. tests/test_cloud_render_copy_link.py proves the current behavior: a non-admin is denied; after verified Drive delivery and removal of the local MP4, the existing file link returns 404. **It does not yet supply a general owner-authorized Drive-aware Concat Uploader input.** This is an integration requirement, not an already shipped feature.

## 3. Copyright Uploader reuse map and risks

| Surface | Source / exact existing behavior | Concat security requirement |
| --- | --- | --- |
| Owner entity relationships | apps/copyright-uploader/part01/lum_engine/studio_tenants.py: studio_entity_owners with (entity_kind,entity_id) -> owner_username. require and allowed_ids enforce copyright-owner boundary; unassigned entities are not listed. | Preserve owner-only decisions for all roles, plus an app namespace for Concat if reusing the shared data store. Prevent B IDs from appearing in A's forms, imports or status APIs. |
| **Critical admin bypass** | studio_tenants.py lines 77-99: is_admin bypass is permitted whenever LUM_ENGINE_INSTANCE != copyright. standalone_app.py lines 1060-1066 similarly bypasses project owner for non-copyright admins. | A new LUM_ENGINE_INSTANCE=concat would re-enable bypass. Explicit strict Concat policy is mandatory; do not depend on literal copyright string alone. |
| Queue/history | standalone_app.py get_projects/get_logs/stats_payload lines 248-281; project_store.py owner_username, owner-specific pause controls and event filtering. | Project identity and query predicates must be **owner + app=concat**. An admin may not inspect another owner in the normal Concat view. Do not mix Copyright and Concat histories or queue start. |
| Per-channel OAuth | standalone_app.py lines 974-1045 requires channel ownership before OAuth start/complete. oauth_web.py stores owner_username in OAuth session and validates it on completion; copyright_vault.py stores owner-scoped tokens. | Preserve channel + session owner checks, exact channel authorization, token path isolation and CSRF/state validation. Shared Google OAuth client JSON must never imply shared user/channel tokens. |
| Client JSON | oauth_web.py owner-client fallback to shared client, standalone_app.py lines 956-971 | Reuse validated shared client and private override, no credential export, no token disclosure in HTML, JSON, logs, browser headers or errors. |
| Sheets | standalone_app.py lines 1249-1411 checks source owner for save/sync/delete; studio_tenants.py checks row-to-channel ownership. | App+owner-configured Sheets sources, row mapping, status history and writeback. Never authorize channel IDs using Sheet content alone. An import cannot attach another owner's channel. |
| Source and cleanup | copyright_handoff.py / copyright_vault.py owner file paths and verification. | Concat-only source is a trusted authorized completed-video link. The resolver must reject foreign/expired IDs, arbitrary URL fetch, loopback/private IP, redirects, path traversal and stale hashes. Retain/pin canonical MP4 until upload verification or safely retryable failure; no cross-owner deletion. |
| Worker claims | project_store.py next_eligible/claim_next_eligible, worker.py, desktop_core/modules/db/queue_repository.py | Add app-scoped admission/claims, manual Start default, duplicate suppression, crash reconciliation and retry history. Stop/pause operations must not manipulate unrelated uploader jobs. |

**App namespace uniqueness:** Existing projects have owner_username but no Concat application discriminator (project_store.py lines 45-116). Copyright's per-owner pause table is not a Concat queue. Implement separate app-aware state or an equivalently isolated persistent adapter with explicit ownership. Reusing a worker is allowed; reusing its queue without scoping is not.

## 4. Legacy-data preservation and migration specification

1. **Inventory read-only first.** Back up SQLite files consistently (SQLite backup API / verified snapshot), capture schema and counts for production_jobs, files, staged ZIPs, final MP4s, Drive paths, config, Copyright projects/channels/Sheets and orphan records. Do not move/delete any data during audit. Production inventory remains **unverified** pending authorized host review.
2. **Additive schema, idempotent migration.** Add owner identity to Concat job/media records and app namespace to uploader records using transactional changes with migration version. Prefer a stable Scene account ID plus normalized username snapshot if the existing identity system supports it; document rename/recreation handling. Maintain original job IDs, remote_id, hashes, queue state and file paths.
3. **Proven attribution only.** Resolve historical owner based on authenticated creation evidence, recorded account association and verifiable operator review. Never derive owner from filename, Drive title, arbitrary URL, __default__, incoming header or an admin login. Do not copy Scene's legacy automatic-primary-admin assignment into Concat.
4. **Quarantine unresolved records.** Ownerless/admin-era/ambiguous items remain intact but absent from all normal Concat lists, link resolution and worker admission. A separately auditable manual assignment interface may be planned later; no silent claim or transfer. Admin's standard owner view remains isolated.
5. **Media linkage integrity.** An output inherits ownership from a verified production input/job; mismatched owner, job, hash, Drive reference or source retention blocks admission. In-progress render keeps pinned endpoint/credentials and expected hashes across restarts.
6. **Recovery and rollback.** Dry-run count and checksum comparison first; perform migration with backup and rollback plan only in a later authorized integration phase. Re-running must not create duplicate owners, links or queue entries; failure restores DB state without deleting media or revoking credentials. Preserve canonical Drive Bridge configuration throughout.
7. **Release gating.** Keep Cloud Render admin-only until all migrated/unresolved records have an enforceable disposition and negative security tests pass. Never open access merely because navigation is visible.

## 5. Agent 2 and Agent 3 handoff — mandatory contract

### Agent 2 (navigation and route ownership)

- Route /concat-studio/ and /concat-studio/concat/ to the existing Cloud Render implementation, not a duplicate app; keep /cloud-render/ bookmarks and existing /api/cloud-render/* calls compatible.
- Route /concat-studio/concat-uploader/ to the existing uploader backend via a trusted Scene-session facade or equivalent protected SSO, with inbound identity headers discarded and re-created by the server. Do not use an untrusted direct upstream URL.
- Map Concat Studio as its own grant; do not piggyback on Copyright. Preserve current Experiment entries unrelated to Concat.
- Do not weaken app.py middleware admin guard or cloud_render.py register_cloud_render admin handler before owner migration/tests. Navigation can be reviewed separately; non-admin working access is NOT authorized by this audit.
- No change to production permissions, GPU renderer, bridge, deployments, main or active jobs.

### Agent 3 (uploader ownership and source admission)

- Resolve the **actual** Scene-authenticated username server-side. For each API and queue operation, require owner == authenticated user and app == concat. Admin is not exempt.
- Reuse Copyright channel, OAuth client, token vault, Sheets and YouTube services, but explicitly eliminate generic-instance admin bypass for Concat; do not rewrite Copyright behavior.
- Source adapter accepts only the approved same-host Cloud Render video-link format; never general-purpose download. On admission, resolve the source under the requester's session, ensure verified completed MP4/authorized Drive provenance/sha and active retention pin, then persist immutable source identity.
- Bind channel ID to owner before OAuth or upload; bind Sheets source and all imported channel mappings to owner; mask tokens. Shared client JSON remains safe to reuse but per-channel tokens must not be shared.
- Add Queue is not Start Queue. Persistent manual-start state must survive restart and be owner+app-specific. Use existing worker with scoped claims and receipt reconciliation; preserve attempted-upload state on ambiguity.
- Retain verified source through retries; only successful verified YouTube upload permits cleanup of transient staging, never delete canonical Concat media or another user's file. Enforce correct timezone/private+publishAt YouTube scheduling semantics.

### Shared authorization abstraction to agree before Agent 2/3 merge

- **Identity:** canonical Scene user verified on server; username casefolded consistently; no client owner_id.
- **Resource:** opaque stable ID, immutable owner, app namespace, status, verified checksum, source provenance.
- **Owner check:** WHERE id=? AND owner=? AND app=? for reads and mutation transactions; 403/404 with no cross-user metadata.
- **Trusted internal service:** worker/service identity plus job capability bound to owner, job, size/hash, expected state and expiration/replay controls. Bearer worker token alone never authorizes a foreign job.
- **Protected media:** requester's session + owner-scoped asset resolution or short-lived scoped capability that is verified again; never expose raw VPS paths or raw Drive credentials.
- **Operational secrets:** shared infrastructure credentials may be controlled centrally but must never be returned to a tenant, and changing an owner's GPU settings cannot redirect another owner's render.

## 6. Negative security acceptance matrix (future implementation gate)

Two simulated non-admin users A/B and third user C=admin acting normally. Create separate A/B ownership for render job, completed local MP4, Drive-moved asset, GPU config, uploader queue/history, Sheet, Channel, OAuth token and thumbnail.

| Scenario | Required result |
| --- | --- |
| A enumerates render jobs/media/history; guesses B/legacy IDs | Only A rows; B/legacy read, preview, download, link, rename, Drive action, cancel, retry and delete return 403/404 without names, hashes or paths. C also cannot read B via ordinary APIs. |
| A copies B's link, changes path/query, opens in a new session, forwards it to uploader | Rejected, including stale/expired/revoked/deleted and Drive-moved assets; original source remains untouched. |
| A sends guessed B ID to POST production/jobs/{job}/{action}, global queue start/pause and GPU config | No cross-owner state changes. In-flight B worker retains its original pinned connection when A changes own config. |
| Forged X-Auth-User / X-Is-Admin, stale session, direct backend URL, missing/invalid CSRF | Rejected by trusted ingress or app, including legacy /cloud-render routes and private upload paths. |
| Worker callback with valid worker credential but wrong job/owner, unknown ID, forged size/hash, duplicate or replayed chunks | Fails closed before media mutation; no cross-owner output publication or overwrite; legitimate retry remains idempotent. |
| A rescans B's Drive file ID, resource key or ZIP; B owns another same-origin ZIP | No B existence leak or mutation; duplicate identity is scoped per owner. |
| A selects B channel, OAuth session, client token, Sheet ID or Sheet-imported channel | Denied for every API and status/HTML list. Owner A's Google client may be shared, but never B's token. |
| A controls B's upload queue/history or starts upload by syncing Sheets | Denied. Sheet sync only updates owned rows; admission is manual Start; no cross-app claim, even after restart. |
| A and B submit same file and destination independently | Correct owner+source+channel duplicate prevention; foreign ownership never blocks by leaking identity. Retry after ambiguous remote result requires reconciliation, not blind re-upload. |
| Media moves to Drive during Concat upload admission, or A cancels/fails while B is active | Verified canonical Drive fallback works through existing bridge; pin/retention prevents premature cleanup; failed/cancelled source preserved. |
| Legacy migrations run twice, interrupted midway, then rolled back | No extra records, ownership transfer, credentials exposure, missing job ID, orphaned media, or queue loss. Ownerless rows remain quarantined. |
| Admin/non-admin parity and SSO navigation | Same owner-level controls after gate clearance, no cross-owner API; old bookmarks safe and unrelated Studio permissions unchanged. |

**Test implementation already added on this audit branch:** tests/test_concat_studio_isolation_audit.py. Uses two simulated Scene sessions, a third admin, temporary Cloud Render SQLite/media and isolated Copyright StudioTenants SQLite. It verifies the current protective admin gate, CSRF and worker-token rejection; tests cross-owner channel/Sheet/group/profile ownership including admin and unassigned records. It intentionally records that the current admin can list all Cloud Render media: **this is a blocker, not a multiuser pass**. Remaining integration cases above cannot safely be marked passing until isolation logic exists.

**Existing regression suites to retain:** tests/test_cloud_render_copy_link.py, test_cloud_render_v1.py, test_cloud_render_production.py, test_three_studio_uploader_tenants.py, test_studio_tenant_http.py, test_copyright_owned_sheet_groups.py and production security/navigation suites; cover worker/Drive and other uploader regressions in integration CI.

## 7. Blockers and release decision

**P0 — block access expansion:** ownerless Cloud files/jobs, global read/write APIs, shared GPU config/queue state, owner-unbound ingest, global Drive deduplication, current Copy Link's admin/local-only behavior, generic uploader admin bypass when using a new instance name, no app-scoped Concat uploader queue/history.

**P1 — integration hardening:** owner-aware legacy inventory/attribution and idempotent migration, pinned Drive-backed source handoff/retention, secure SSO routing, OAuth/Sheet mapping and queue claim scoping, timezone-safe YouTube scheduling.

**Unknown until authorized host inspection:** which historical jobs/media are truly ownerless, producer/recipient account evidence, current credential associations, queue occupancy, filesystem permissions and actual production behavior. No conclusions about these runtime facts are claimed.

**Security release gate:** Before allowing any non-admin Concat access: implement strict backend owner+app checks, migration/quarantine, callback binding and authorized media links; run actual two-user API/integration and negative tests with admin as a third ordinary owner; prove all checks in CI and targeted staging; only then separately authorize guarded production deployment. No audit branch merge or deployment is authorized here.
