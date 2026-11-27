# ARCHIVED VPS terminal audit (kept for PR #12 traceability)

**Historical audit (2026-11-19):** The consolidated authentication implementation used PR #13's standalone `CONCAT_AUTH_MODE=local-admin`, not PR #12's proposed `local` mode. **As of 2026-11-22, `main` is the canonical Concat Web release source; the former `integration/personal-release-20261119` code has been merged into `main`.** Statements below about integration-only policy, SSO blockers, and the VPS state describe the historical audit, not current release instructions. Follow `deploy/RELEASE_GATES.md` and verify the live host state independently.

---

# Concat Web: manual VPS terminal deployment audit — 2026-11-19

**Dedicated work branch:** `ops/terminal-deployment-audit-20261119`  
**Source repository:** `dilshadshahsyed883-cmd/concat-web`  
**Deployment policy:** All changes committed to a feature/ops branch and reviewed in a PR. Production release uses a tested SHA from protected `main` and a manual VPS terminal operation; GitHub Actions automatic deployment is not desired. The existing VPS_1 website, Nginx routes, Drive Bridge, OAuth, Salad and data remain unchanged.

This is a record of **user-supplied terminal output**, not independently executed VPS inspection.

## Read-only baseline before bootstrap

| Check | Result |
|---|---|
| Nginx config | PASS |
| Port 18873 | FREE |
| `concatweb` Linux user | MISSING |
| `concat-web.service` | MISSING |
| `/usr/local/sbin/concat-web-deploy` | MISSING |
| `/etc/concat-web/concat-web.env` | MISSING |

## Bootstrap successful (service NOT started)

Pinned source commit: `0a4595995308ce147eac5bac7f25ad270d7bd17a`.

The operator downloaded two existing deployment files from this exact commit:
- `deploy/guarded-release.sh`; Git blob SHA `9eb546b7e27ea579fca2196878ff339af9f71a25`
- `deploy/concat-web.service`; Git blob SHA `6dade198aac2b555307511d7f2ee6d00ee86b723`

Both `git hash-object` checks passed. Shell output:

```text
SOURCE_VERIFICATION=PASS
CONCAT_BOOTSTRAP=PASS
CONCAT_SERVICE=INSTALLED_NOT_STARTED
SCENE_SSO_SCHEMA=DB_NOT_FOUND_AT_EXPECTED_PATH
```

Successful script execution means `concatweb`, private Concat storage paths and release directory, root-owned deployment helper and independent systemd unit were set up. The script did NOT create the private environment file, run a release, start Concat, or modify Nginx.

The initially guessed Scene DB path `/opt/scene-builder-web/data/web.sqlite3` does not exist. This does **not** prove Scene SSO tables are missing elsewhere.

## Targeted Scene service and database discovery (read-only)

Reported active systemd services:
- `vps1-scene-candidate.service`, running; `WorkingDirectory=/srv/fresh-website/current/scene-builder/part01`.
- `vps1-scene-production-worker.service`, running.
- `vps1-scene-worker-admission.service`, running.
- Other VPS1 production LUM/song/short services are running.

An explicit SQLite scanner searched under `/opt`, `/srv`, and `/var/lib`, skipping `.git`, `node_modules`, virtualenvs and Concat own storage. It inspected 90 candidate DBs and **stopped after finding 12 matching databases**. The 12 results are all under historical `/srv/fresh-website/transactions/.../backup/` or `.../sqlite-backup/` paths. Each has a `users` table and none has `concat_sso_sessions`. Therefore the scan is **not an exhaustive survey** and cannot be used to conclude that the **live** Scene database or SSO schema is absent.

Important: Do not select a transaction backup SQLite file as the SSO authority. Determine the database path from the active Scene service's actual configuration and verify the schema against that active DB **read-only**, as `concatweb`. Do not show env values, tokens, keys, or SQL row data in terminal screenshots or GitHub.

## VPS OS notice

The VPS login banner reported pending package/security updates, Ubuntu 26.04.1 LTS upgrade available, and a required restart. These are maintenance notices, **not evidence** of a Concat application failure. No blanket `apt upgrade`, `do-release-upgrade`, service restart, or server reboot should be mixed into Concat bootstrap.

## Release blockers

1. Determine the actual live Scene users DB path, then verify `users(username,is_active,is_admin)` and `concat_sso_sessions(sid_hash,username_key,expires_at,revoked_at)` read-only, including access by `concatweb`.
2. Verify upstream Scene SSO issuer is **deployed live**, with exact signing key and session revocation support; code merged in GitHub alone is insufficient.
3. Create root-owned `0600` `/etc/concat-web/concat-web.env` with the **already-approved** cross-app SSO key and app secrets, owner-scoped persistent storage and HTTPS origins. Never save secrets to repo/chat/logs.
4. Run the existing **installed** `/usr/local/sbin/concat-web-deploy preflight <verified-main-SHA>`. It fails closed when prerequisites are missing.
5. Only after preflight, CI and live SSO acceptance: package the exact tested `main` SHA, upload via a secure terminal channel and invoke the installed guarded helper to stage the independent loopback service. Public Nginx route/TLS cutover is a **separate** guarded step.
6. Verify Concat-only /healthz, denial of anonymous private routes, two-owner/admin SSO and revocation, and a controlled GPU-to-verified-MP4-to-uploader acceptance test. Preserve independent rollback and prior releases.

**Current state:** bootstrap complete; no Concat release or public activation; live SSO DB path unresolved; private environment unprovisioned; existing VPS1 production preserved as far as operator-reported steps show.


## New finding: authoritative Scene candidate runtime (read-only)

User-supplied VPS terminal probe inspected the active `vps1-scene-candidate.service` unit and the runtime's effective `WEB_DATA_DIR` (without printing credentials). It reported:

```text
SCENE_PID_RUNNING= True
SCENE_APP_PATH= /srv/fresh-website/current/scene-builder/part01
SSO_ISSUER_FILE= False
SCENE_DB_PATH= /var/lib/fresh-website/scene-builder/web/web.sqlite3
SCENE_DB_EXISTS= True
USERS_TABLE= True
CONCAT_SSO_TABLE= False
VPS_CHANGES=NONE
```

**Correct live Scene DB:** `/var/lib/fresh-website/scene-builder/web/web.sqlite3`.  
**Important blocker:** the currently running candidate code tree does not contain `concat_sso_issuer.py`, and this live database has no `concat_sso_sessions` table. Hence the Scene issuer integration is **not deployed/initialized in the inspected candidate**. Upstream VPS1 SSO issuer PR #773 is merged in source, but a source merge is not proof of live service deployment. No safe route exists to bypass this blocker in Concat Web.

### Impact and next steps

- **Do not start or expose Concat Web yet;** login must remain fail-closed.
- Resolve the upstream SSO release through the **VPS1 repository's separate controlled deployment** after verifying it is ready. Preserve VPS1 production; do not copy issuer Python modules into the live host manually, run schema-changing SQL manually, or turn off SSO/security gates.
- Once upstream is live, confirm that `concat_sso_issuer.py` is present in the active code release and the authoritative Scene DB contains `concat_sso_sessions`. Then confirm schema and read-only SQLite WAL access from `concatweb`.
- Provision the private Concat environment only after aligning the approved cross-app signing secret, runtime path, and isolated settings. Run Concat preflight (without deployment) before any loopback release.
- The Ubuntu restart/upgrade banner is unrelated to this verification; don't reboot/upgrade as part of this work.

This finding **supersedes the earlier guessed DB path** and clarifies why the scan of 12 transaction backup databases was not diagnostic of the live issuer.


## Subsequent Scene release-readiness audit — 2026-11-19

The operator ran a second **read-only** check of the active Scene candidate and its existing release/deploy helper. Output:

```text
CURRENT_RELEASE=/srv/fresh-website/releases/scene-builder/9379c166026794994a64ffaffe4efaf3812c02ed
VPS1_DEPLOY_HELPER=READY
SCENE_SSO_SECRET_IN_ENV_FILE=MISSING
SCENE_HEALTH_HTTP=200
SCENE_WORKER=active
CHANGES=NONE
```

Interpretation:
- Scene is serving its current release and responding to `http://127.0.0.1:18080/health` with HTTP 200; the production Scene worker is active.
- The existing VPS1 production deploy helper `/usr/local/sbin/vps1-production-deploy` is executable, but this **does not authorize invoking it for an unverified release**.
- The tested file `/etc/fresh-website/scene-builder.env` does not contain a line matching `^CONCAT_SCENE_SSO_SECRET=.{32,}$` (or that file is absent). This is a **file-specific** result: it does not establish whether the secret is provisioned through another unit environment source or secret manager. Never print the secret's value.
- In the immediately preceding runtime audit, the active candidate code lacked `concat_sso_issuer.py` and the authoritative live DB lacked `concat_sso_sessions`. Those remain blockers despite HTTP 200.

### Next guarded action

Verify the correct active Scene service configuration and approved secret provisioning path **without exposing environment values**. Confirm the upstream issuer release was tested and approved independently in `vps1-deploy-control`; use only the established VPS1 guarded deployment/rollback process, never manual SQL migration or bypass. After the authorized upstream rollout, recheck the issuer file, SSO table, live session revocation, and Concat user read-only DB access. Only then proceed with Concat's private env, strict preflight and loopback deployment.

**Do not modify Scene services, perform an Ubuntu upgrade/reboot, start Concat, or open public Concat ingress based on these read-only audits alone.**


## Attempted guarded upstream Scene release — FAILED (2026-11-19)

Operator invoked the existing host-installed VPS1 helper from the VPS terminal, targeting exact GitHub `main` SHA `6a34d5a34f3974dea0c4a3f919c3ae5fe9b2e53d` with `install <sha> scene-builder`. The operator's shell confirmed remote `main` matched that SHA and live Scene health checks passed **before** release.

A private `CONCAT_SCENE_SSO_SECRET` was generated and inserted into `/etc/fresh-website/scene-builder.env` with an on-host, root-only backup `scene-builder.env.before-concat-sso`. Output reported `SCENE_SSO_SECRET=CONFIGURED`. **The secret's value was not printed and must never enter GitHub.** This is an actual VPS configuration change, unlike previous read-only audits.

**Observed release failure:**

```text
SCENE_WORKER_HOME_ALREADY_VERIFIED=PASS
SCENE_WORKER_HOME_ROLLBACK=PASS PROJECTS=PRESERVED
RuntimeError: Scene production worker: Scene builtin LUT preparation/FFmpeg proof failed:
ModuleNotFoundError: No module named 'smart_scroll'
...
CalledProcessError: ... production.runtime install --revision 6a34d5a34f3974dea0c4a3f919c3ae5fe9b2e53d --apps scene-builder
```

There were also multiple Python `SyntaxWarning: invalid escape sequence '\\,'` messages. The exception in the LUT proof, rather than the warnings, is the reported fatal error.

**Source investigation (same exact VPS1 SHA):**
- `production/scene_lut_assets.py` invokes the unprivileged `vps1scene` worker's Python with `PYTHONPATH=<candidate>/part02` and imports `smart_scroll.lut_engine`.
- The tracked source actually contains `apps/scene-builder/part02/smart_scroll/__init__.py` and `smart_scroll/lut_engine.py`.
- `production/scene_candidate_release.py::_stage` calls `target.mkdir(parents=True)` and child `path.parent.mkdir(..., exist_ok=True)` without explicit directory permissions.
- The operator ran `sudo bash` with `umask 077`; if inherited, newly created **root-owned release directories** may be mode `0700`, making the otherwise-present package inaccessible to `vps1scene`. This is a **strong hypothesis, not yet proven**; read-only `namei -l`/worker import evidence is required. Another possibility is wrong/incomplete stage mapping.

**Important: failed install does not prove successful rollback or current service state.** A worker-HOME rollback message covers only that operation. Check current candidate symlink, service health, issuer file, DB schema and permissions before any retry. No Concat Web deployment occurred.

### Safe read-only diagnosis

```bash
sudo bash <<'SH'
set -Eeuo pipefail
SOURCE='/srv/fresh-website/releases/scene-builder/6a34d5a34f3974dea0c4a3f919c3ae5fe9b2e53d/part02'
echo '=== SOURCE AND PATH PERMISSIONS ==='
test -f "$SOURCE/smart_scroll/lut_engine.py" && echo 'LUT_SOURCE=EXISTS' || echo 'LUT_SOURCE=MISSING'
namei -l "$SOURCE/smart_scroll/lut_engine.py"
echo '=== WORKER ACCESS ==='
runuser -u vps1scene -- test -r "$SOURCE/smart_scroll/lut_engine.py" && echo 'WORKER_FILE_READ=YES' || echo 'WORKER_FILE_READ=NO'
runuser -u vps1scene -- /usr/bin/env -i PATH=/usr/bin:/bin PYTHONPATH="$SOURCE" /srv/fresh-website/venvs/scene-production-worker/bin/python -c 'from smart_scroll import lut_engine; print("WORKER_IMPORT=PASS")' || echo 'WORKER_IMPORT=FAIL'
echo '=== POST-FAILURE HOST STATUS ==='
systemctl is-active vps1-scene-candidate.service || true
readlink -f /srv/fresh-website/current/scene-builder
curl -sS -o /dev/null -w 'SCENE_HEALTH_HTTP=%{http_code}\n' --max-time 8 http://127.0.0.1:18080/health || true
echo 'SSO_ISSUER_FILE_CHECK='
test -f /srv/fresh-website/current/scene-builder/part01/concat_sso_issuer.py && echo PRESENT || echo MISSING
echo 'CHANGES=NONE'
SH
```

**Do not** apply arbitrary `chmod -R` to production trees, pip-install `smart_scroll` from PyPI, inject `sys.path`, skip LUT proof, restart services or retry the release blindly. If permissions are confirmed, fix the immutable source staging permissions **in the VPS1 source repository with tests and a separate PR**, then use the normal guarded release after CI. Keep this Concat-Web PR as the cross-app audit record.


## Confirmed root cause and upstream fix PR — 2026-11-19

Operator executed the restricted read-only inspection against failed candidate SHA `6a34d5a34f3974dea0c4a3f919c3ae5fe9b2e53d`:

```text
MODULE_FILE=EXISTS
# namei -l reported:
drwx------ root root 6a34d5a34f3974dea0c4a3f919c3ae5fe9b2e53d
drwx------ root root part02
drwx------ root root smart_scroll
-rw-r--r-- root root lut_engine.py
WORKER_READ=FAIL
ModuleNotFoundError: No module named 'smart_scroll'
IMPORT=FAIL
SCENE_CANDIDATE_SERVICE=active
CURRENT_SCENE_RELEASE=/srv/fresh-website/releases/scene-builder/9379c166026794994a64ffaffe4efaf3812c02ed
HEALTH=200
CHANGES=NONE
```

This **confirms** restrictive staged directory modes `0700`, not an absent Python file, caused the worker import failure. Scene candidate stayed on the prior healthy release. The pre-release SSO secret was written into Scene's root-owned private environment with a backup; the attempted source release failed before the observed promotion.

**Working code fix (separate upstream repository and draft PR):**
- VPS1 repository: `dilshadshahsyed883-cmd/vps1-deploy-control`
- Branch: `fix/scene-stage-traversal-permissions-20261119`
- Draft PR: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/774
- Change: explicit staged directory `0755` modes after immutable manifest verification on both fresh and re-entered staging; reject symlinks; retain `0644` source files.
- Regression tests target restrictive `umask 077`, repeated stage verification and symlink refusal.
- Not yet proven CI-passing or deployed: **do not retry the old SHA or manually chmod the VPS release**. Validate upstream PR tests and reviewed merge, then perform a **new SHA-pinned** guarded Scene deployment with preflight and rollback. Recheck Scene issuer and `concat_sso_sessions` before Concat's own installation.

`concat-web/main` and VPS production were **not modified** by the GitHub code changes recorded here.


---

## Deployment direction superseded: personal single-admin fast path — 2026-11-19

**User decision:** Concat Web is a **personal-use application**. For the initial Concat release, do **not** require the VPS_1 Scene SSO issuer, its database, its cross-app signing secret, or multi-user provisioning. This decision **supersedes the SSO-dependent future release recommendations above**; the historical terminal/audit findings remain preserved as evidence. **It does not assert that the code already implements the new auth path.**

### Approved architecture for the next Concat release

- One independently authenticated **local administrator** for Concat Web, provisioned explicitly in the app's root-owned private environment/state. No public signup and no default/demo password. Do not trust identity headers or use a static hard-coded/bearer-token bypass. Use a salted, slow password hash, authenticated revocable sessions, secure HttpOnly SameSite cookies, CSRF protection for state changes, strict HTTPS/origin checks, rate-limited login attempts and logout/session revocation. Do not print/store passwords or signing secrets in GitHub or chat.
- Retain Concat's current owner-scoped media/job/queue isolation predicates and deny unknown/ownerless legacy records. For this first release only, one configured administrator is sufficient; do not build a new multi-user authorization or account management system. All Concat operations should resolve to the configured owner, not to an unverified request parameter.
- **No new VPS_1 Scene deployment, SSO issuer/schema/secret coordination or changes to the upstream Scene app are required for this new target.** The prior VPS_1 staging-permission/LUT failure and upstream PR #774 are historical findings, not Concat personal-mode prerequisites. Do not undo any prior approved VPS1 configuration; simply avoid further coupling.
- Keep Concat a separate Linux user, state store, loopback service and Nginx hostname (`concat.scenebuilder.online`). Preserve existing VPS_1 production/other Nginx routes, Salad GPU, OAuth/channel data, verified MP4 handoff and canonical Drive Bridge access rules. No data migration or cross-owner mixing.
- **GitHub-first development, manual VPS terminal release only:** commit/test changes in `concat-web`, review the exact protected `main` SHA, take independent backup, run guarded Concat-only preflight, stage/start Concat on loopback, verify health and denied unauthenticated endpoints, then perform a separately guarded HTTPS/Nginx cutover with rollback. Do not use GitHub Actions to deploy. No merge/cutover is authorized by this documentation decision alone.

### Required code work before deployment (not completed by this docs-only PR)

1. Implement/test independent local-admin authentication and session/CSRF handling in `app/modules/admin/security.py`, `app/modules/admin/routes.py` and `app/auth.py`. Replace the Scene login/callback URLs with a Concat-local login page/flow. Preserve fail-closed behavior until setup is valid; remove Scene reads only in the **new** explicit personal-mode path, not via blanket auth bypass. Keep the owner principal stable and the API contract for dashboard/Concat/Uploader intact.
2. Update `.env.example`, `deploy/guarded-release.sh` and `deploy/RELEASE_GATES.md` to verify a strong private local-login setup instead of the live Scene DB/SSO issuer. The **currently committed deployment helper still requires Scene SSO** and therefore cannot deploy this new mode yet. Also verify the installed host helper matches the revised reviewed code before using it.
3. Update/extend focused tests for successful local login and denial of anonymous access, malformed credentials, login brute force, session expiry/revocation, CSRF and cross-owner/legacy access. Confirm the existing uploader auth handoff and active Cloud Render→verified MP4→Uploader workflow, not only an HTML page load.
4. Validate independent service health, private state, backups/rollback and exact SHA; check existing app health after guarded cutover. Leave automated deployment OFF. **No deploy, merge, upstream modification, secret generation or VPS action is performed in this PR update.**

**Status at this decision:** design approved for faster deployment; **code migration, testing and actual deployment remain outstanding**. This PR remains a traceable deployment audit/decision record. Do not mark the SSO-bound current implementation production-ready merely because the intended future architecture has changed.


## Architecture decision superseding Scene SSO — 2026-11-19

The owner explicitly approved **permanently replacing Scene SSO** with
**standalone single-admin Concat authentication** to deploy the already completed
Concat Web independently. The earlier Scene-issuer dependency and upstream
VPS1 PR #774 are **no longer Concat Web release blockers**. Do not touch
LUT, GPU engine, worker, Scene app, or other VPS1 services for this change.

**Separate coding PR**: https://github.com/dilshadshahsyed883-cmd/concat-web/pull/13
`feature/standalone-admin-terminal-release-20261119` against `concat-web/main`.

Implemented in draft PR #13:
- Native username/password login using stdlib scrypt password hash, persistent
  local session hashes, secure `__Host-` cookie, anti-CSRF, throttling,
  logout and revocation. No SSO callback/ticket/Scene DB path.
- Independent admin settings; owner-bound Cloud Render, dashboard and uploader
  reused without cross-user/ownerless fallback.
- Local-only environment/host preflight, secure root-only first-run admin
  provisioning, and removal of SSH/Tailscale/auto-deploy from GitHub workflow.
- Uploader core path conflict uncovered by integration CI: copied core must be
  bound to Concat private `LUM_DATA_DIR/core` and OAuth/vault roots, **not**
  `/var/lib/local-upload-manager`. A fail-closed path guard was added.
- Tests cover login, owner isolation, CSRF, session revocation and simulated
  GPU receiver -> verified MP4 -> persistent queue -> dashboard; separate
  clean-process test covers the uploader UI and private path binding.

At commit `90622fd5a0f84a45085170e3eb5d82cac00596c5`,
GitHub Admin security checks and essential checks both completed successfully.
No merge or VPS deployment is part of these PR changes; live Salad GPU,
Google OAuth/YouTube and public TLS acceptance remain for controlled staging
after the approved merge/manual terminal release.

**Current project policy:** GitHub-first code, feature branch/PR, review and
tests, then verified protected `main` SHA manually deployed from VPS terminal
with backup and rollback. Automatic GitHub Actions production deployments OFF.
