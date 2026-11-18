# Parallel feature ownership (frozen foundation interface)

**Source-of-truth policy:** COPY → ADAPT → VERIFY first-party VPS_1 code already copied under `reused/`. No direct cross-repository source imports, no unnecessary rewrite. Source filenames and upstream PR refs are documented in `docs/REUSE_MANIFEST.md`.

| Agent | Exclusive modifications | Existing copied starting point | Runtime APIs it owns |
|---|---|---|---|
| 2 Concat Studio | `app/modules/concat/**`, `reused/scene/**` | Cloud Render backend/Production, original template/JS/CSS; keep old Copy Link contract | `/api/v1/concat/**`, `/api/cloud-render/**` scoped compatibility |
| 3 Concat Uploader | `app/modules/uploader/**`, `reused/uploader/**` | PR #770 Concat instance, worker, studio tenants, existing templates/assets | `/api/v1/uploader/**`, `/concat-uploader/**` |
| 4 Dashboard | `app/modules/dashboard/**` | Studio job/queue summary contracts | `/api/v1/dashboard/**` |
| 5 Admin & Security | `app/modules/admin/**`, `app/auth.py` | PR #768 audit, Scene SSO | `/api/v1/admin/**`, production verified identity resolver |

Core owned by Foundation: `app/main.py`, `app/config.py`, `app/contracts/**`, `app/ui.py`, `app/templates/**`, `app/static/**`, `deploy/**`, workflows. Agents may add their own templates/static inside feature package, without overwriting shared core. New feature routers are already included in main and may be extended in place. Route modules must preserve `router` exports.

**Contract:** Backend `current_principal` must resolve a verified, active Scene session with `concat_granted=True`, never raw header identity. `Principal.username` is the only owner input. Admin feature route uses `admin_principal`; normal admin Concat renders/uploads are **still owner-scoped**. Until Agent 5 completes secure identity, protected routes fail closed; no bypass permitted.

**API shape:** Standard JSON objects with `id`, owner-scoped status, verified `media_id`/SHA256. `RenderRecord`, `VerifiedMedia`, `UploadRecord`, `DashboardStats` typed contracts defined in `app/contracts/models.py`. Owner is always server-derived, not a query parameter. Add pagination using opaque `cursor` and bounded `limit`.

**Agent 2 integration:** Do not copy the entire Scene `app.py`; bind the copied Cloud Render route manager into the new application through a secure router adapter, preserving existing API endpoints and Media/Production/GPU Config UI. Existing global admin-only ownerless Cloud renderer cannot be exposed to non-admin users. Auth/owner/schema tests are a hard release gate.

**Agent 3 integration:** PR #770 is a source-only variant of the existing uploader. Retain the `lum_engine` and `desktop_core` directory shapes together under `reused/uploader/part01`. Its runtime must be isolated, configured for Concat and exclusively accessible after verified SSO. A copied valid video link is an identifier, not authorization.

**Agent 4:** Dashboard render/upload stats require reliable authenticated owner-filtered APIs; no synthetic/fake counters or direct database access without owner predicate.

**Agent 5:** Implement verified trusted-session resolver and role activation/revocation checks, CSRF, admin route separation and negative cross-owner tests. No login-headers-only shortcuts. Coordinate upstream Scene SSO support by a separately reviewed VPS_1 change; do not patch production website from this repository.

**Cross-agent dependencies:** PR #768 is an AUDIT, not owner-schema implementation. Check reused code for ownerless Cloud records and isolate/quarantine before enabling. Canonical Drive Bridge must preserve existing rclone identity, checksums and cleanup rules. Agent 1 has not deployed or modified VPS_1.
