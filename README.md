# Concat Web

Independent FastAPI application deployed separately on VPS_1. Its existing
Concat Studio, GPU render receiver, verified MP4 library, Concat Uploader,
Google Drive integrations, and dashboard remain in this repository. The
existing VPS_1 Scene website remains unchanged.

**Authentication decision (2026-11-19): standalone single administrator.**
Scene SSO is no longer a dependency. No Scene account DB, Scene session,
SSO issuer or VPS1 PR #774 is needed for Concat Web. Every request uses a
revocable server-side local session; owner-scoped media and queue isolation
remain mandatory even for the administrator.

## Product

Top-level navigation: Dashboard, Concat Studio, Admin.
Concat Studio contains Production, Media, GPU Config, and Concat Uploader
(Sheets Sync, Channels, Upload Engine and Queue).

The dashboard reads real owner-scoped job and uploader stores, not mock stats.
Media must be checksum-verified before copy-link/uploader admission. Legacy
global or ownerless data is quarantined; no admin override or silent migration.

## Standalone login

**Login link:** [https://concat.scenebuilder.online/auth/login](https://concat.scenebuilder.online/auth/login)

Use the standalone Concat administrator username and password configured on the VPS.
Successful login opens [Dashboard](https://concat.scenebuilder.online/dashboard).
An already signed-in browser also redirects from the login link to Dashboard;
use a private/incognito window to see the login form without changing that session.

- Only `CONCAT_AUTH_MODE=local-admin` may enable local identity.
- Root-owned private environment contains `CONCAT_ADMIN_USERNAME`,
  `CONCAT_ADMIN_PASSWORD_HASH` (scrypt, not plaintext),
  `CONCAT_ADMIN_SESSION_SECRET`, and separate uploader secrets.
- `GET /auth/login` issues a secure login CSRF nonce.
  `POST /auth/login` verifies the nonce, origin, username/password,
  and applies rate limiting; successful login sets a secure `__Host-`
  session cookie with a hashed/revocable SQLite session record.
- `POST /auth/logout` requires a verified session plus anti-CSRF token;
  local admin may revoke all sessions. Password-hash rotation invalidates
  previously minted sessions.
- Browser identity headers, Scene cookies and foreign owners are rejected.
- Public `GET /healthz` provides minimal unauthenticated health response.

## Offline verification (no VPS writes)

```bash
python -m pip install -e '.[test]'
python -m compileall -q app reused run.py
python -m unittest discover -s tests -p 'test_foundation.py' -v
python -m unittest discover -s tests -p 'test_admin_security.py' -v
python -m unittest discover -s tests -p 'test_reused_*.py' -v
python -m unittest app.modules.concat.test_routes -v
python -m unittest discover -s app/modules/uploader/tests -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_release_security_contract.py' -v
bash -n deploy/guarded-release.sh
python -m pip wheel --no-deps --wheel-dir /tmp/concat-web-wheel .
```

The integration fixture covers **local admin login -> simulated GPU receiver
chunk -> SHA-verified MP4 -> copied persistent uploader queue -> dashboard ->
logout**, without contacting Salad, Google OAuth or real YouTube. A genuine
GPU/Drive/OAuth acceptance check is required at controlled VPS staging before
claiming full external-service readiness.

## Canonical source of truth (2026-11-22)

**`main` is the sole source of truth for Concat Web.** On 2026-11-22, the complete
`integration/personal-release-20261119` history and implementation were
merged into `main`, preserving the earlier main-only GPU Config cleanup and
the current Salad idle auto-shutdown feature.

- Start new work on short-lived feature/fix branches based on `main`.
- Open pull requests **into `main`**, run essential and security regression
  checks, and merge reviewed changes. Do not make further release commits
  directly to the legacy integration branch.
- Release **only an exact reviewed, CI-verified `main` SHA**. Never deploy a
  feature-branch SHA, legacy integration SHA, or floating `main` label.
- **GitHub Actions is verification only**: no GitHub SSH/Tailscale deployment,
  service restarts, Nginx cutover or secret provisioning. All Concat Web VPS
  staging remains a separate, authorized, manual terminal operation.
- The existing `deploy/manual-integration-release.sh` filename is retained
  for backwards compatibility, but its **branch verification and clone now
  target `main`**. The checked-out source SHA must match the exact SHA passed
  to the script.

See [release gates](deploy/RELEASE_GATES.md) for private environment,
preflight, backup/rollback, loopback staging, and separate HTTPS Nginx cutover.
See [Salad Auto Worker](deploy/SALAD_AUTO_WORKER.md) for the verified-Drive
startup gate, adjustable 8%/20s API image-pull check, bounded reallocation and
60-second idle shutdown, without GPU image changes.
See [Salad idle auto-shutdown](deploy/SALAD_IDLE_AUTOSTOP.md) for the opt-in
1–60 minute timer (default 5) and required dedicated group identifiers.

## Main-only guarded VPS staging

After GitHub checks pass on `main`, use an exact, verified 40-hex `main`
SHA in the VPS terminal. This stages only Concat Web's isolated loopback
service with a private database/config backup and rollback-aware smoke tests.
It does **not** modify the public Nginx site, other VPS services or Salad
GPU image; public HTTPS cutover requires separate checks and authorization.

```bash
SHA=<exact-reviewed-40-hex-main-SHA>
TMP=$(mktemp)
curl -fsSL "https://raw.githubusercontent.com/dilshadshahsyed883-cmd/concat-web/$SHA/deploy/manual-integration-release.sh" -o "$TMP"
sudo bash "$TMP" "$SHA" preflight
# Only after preflight is approved, run: sudo bash "$TMP" "$SHA" stage
rm -f "$TMP"
```

No passwords, hashes, API keys or secrets belong in GitHub or chat. Private
credential bootstrap runs only in the VPS terminal.

## Historical Nginx audit (2026-11-19; reverify before staging)

The 2026-11-19 audit observed a root-owned Concat placeholder
`/etc/nginx/sites-available/concat-web.conf`, linked from
`sites-enabled`. Its HTTPS `/healthz` returns a static HTTP 200 and is
**not** proof that Concat is running. The exact file fingerprint is documented
in `docs/NGINX_ROUTING_AUDIT_20261119.md`.

After an exact-SHA main release is staged and its independent loopback
health/login/401 gates pass, use only `deploy/nginx-adopt-existing.py`
with `audit`, then manually `activate <same-SHA>`. The utility backs up
the existing Concat site and surgically substitutes only the HTTPS
`location /` with `deploy/nginx-proxy-location.conf`, preserving port-80
redirects, ACME, certificate config and other sites. It tests Nginx and
reverts the single Concat file on failed post-cutover checks. It has an explicit
`rollback <same-SHA>` operation.

**Do not use** `deploy/nginx-cutover.sh activate` on this VPS: that tool is
for a new, absent Concat virtual host and intentionally refuses the existing
site. The integration staging archive includes `deploy/` so the existing-site
adoption utility is present under `/srv/concat-web/current/deploy/`.
