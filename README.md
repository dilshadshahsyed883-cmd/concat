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

## Deployment policy

Code changes happen in GitHub feature branches and PRs. **Current release target:** `integration/personal-release-20261119` (not `main`).
After checks pass, pin that integration branch's exact verified SHA and deploy
manually from VPS terminal using the existing Concat-only
`deploy/guarded-release.sh` and independent `concat-web.service`.
GitHub Actions is CI-only and **cannot deploy to VPS**.

See [release gates](deploy/RELEASE_GATES.md) for private environment,
preflight, backup/rollback, loopback staging, and separate HTTPS Nginx cutover.
The 2026-11-18 SSO notes are historical and no longer apply to the current
standalone-login design.

## Integration-only release state (2026-11-19)

The user requested **not to merge into `main`**. The integration branch contains the consolidated PR #13 implementation and preserved PR #12 audit evidence. Only a separately verified, explicitly approved terminal release may use a pinned SHA of this integration branch. This is a conscious temporary exception to the earlier protected-main-only release procedure. GitHub Actions remains CI-only. Keep `main` and existing VPS1 services unchanged.

## Guarded integration-only VPS staging

After this integration commit passes both GitHub CI checks, manually execute `deploy/manual-integration-release.sh` on the VPS with its **exact verified SHA** and `stage` mode. It validates the remote integration branch SHA, clones exactly that code, takes private backups of the existing Concat helper/environment and SQLite state, installs the new local-admin Concat-only helper, provisions admin credentials privately (once, interactively), runs preflight, packages that same SHA and stages on loopback with rollback-aware smoke tests. It does **not** modify Nginx or public HTTPS ingress; that is a separate guarded terminal cutover after host/DNS/TLS validation.

Example (run in the VPS terminal, using the verified integration SHA):

```bash
SHA=<exact-verified-40-hex-integration-SHA>
TMP=$(mktemp)
curl -fsSL "https://raw.githubusercontent.com/dilshadshahsyed883-cmd/concat-web/$SHA/deploy/manual-integration-release.sh" -o "$TMP" && sudo bash "$TMP" "$SHA" stage
rm -f "$TMP"
```

No password, hash, API key or secret may be entered in this chat or GitHub. The credential bootstrap prompts only in the VPS terminal. Do not execute this unless the local VPS hostname, storage, separate service, HTTPS configuration and host/operator identity are confirmed; the script fails closed otherwise.


## Existing Nginx site (audited 2026-11-19)

The live VPS already has a root-owned Concat placeholder
`/etc/nginx/sites-available/concat-web.conf`, linked from
`sites-enabled`. Its HTTPS `/healthz` returns a static HTTP 200 and is
**not** proof that Concat is running. The exact file fingerprint is documented
in `docs/NGINX_ROUTING_AUDIT_20261119.md`.

After an exact-SHA integration release is staged and its independent loopback
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
