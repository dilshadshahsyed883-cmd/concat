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

Code changes happen in GitHub feature branches and PRs. After successful
checks and approval, merge to `concat-web/main`, pin the exact SHA and deploy
manually from VPS terminal using the existing Concat-only
`deploy/guarded-release.sh` and independent `concat-web.service`.
GitHub Actions is CI-only and **cannot deploy to VPS**.

See [release gates](deploy/RELEASE_GATES.md) for private environment,
preflight, backup/rollback, loopback staging, and separate HTTPS Nginx cutover.
The 2026-11-18 SSO notes are historical and no longer apply to the current
standalone-login design.
