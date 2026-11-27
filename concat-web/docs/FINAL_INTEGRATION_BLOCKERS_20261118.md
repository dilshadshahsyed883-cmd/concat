# Concat Web — Final Integration Release Blockers (2026-11-18)

Branch: integration/concat-web-final-20261118
Base: baseline/foundation-verified-20261118
Target PR: main (NO MERGE, NO DEPLOY)

## Implemented in integration branch

- Exact source integration of verified PRs #3 Uploader, #4 Concat Studio, #5 Dashboard, #6 Admin/SSO, preserving original engine and UI rather than rebuilding.
- Main ASGI app registers production dashboard data adapters (real Cloud Render owner DB and copied uploader owner-specific SQLite queue, both read-only).
- Verified Concat Web Copy Video Link uses only SHA-verified ready owner MP4s from the per-owner directory; copied uploader stages private bytes and retains queue duplicate prevention.
- Source admission is restricted to the canonical hashed owner directory; every legacy global single-database source is quarantined, including rows carrying an old owner_username claim. No automatic migration or fallback.
- Dependency pins and copied uploader source/assets included in wheel configuration; minimal existing CI workflows retained.
- Offline integration fixture simulates the Salad receiver's output and verifies checksum/source handoff, queue persistence, owner-specific dashboard, rejected foreign owners, forged session headers, CSRF and legacy quarantine.
- No changes to Scene/VPS_1 repository, production database, deployed web routes, OAuth, Drive Bridge, Salad image or release artifacts.

## Hard release blockers

1. **UPSTREAM SSO ISSUER MISSING.** VPS_1 issue #772 is OPEN. Scene lacks /concat-sso/authorize, revocable concat_sso_sessions and signed one-time tickets. Consumer intentionally denies access without those; NEVER bypass with a header, local password, anonymous Principal, copied Scene cookie or stale role.
2. **No live Scene↔Concat SSO acceptance.** After upstream independently reviewed, verify active user, admin, denial/revocation, logout propagation, two-owner negative cases and secure cookies behind the actual HTTPS reverse proxy. Do not modify prod Scene for this PR.
3. **External services not tested end-to-end.** Offline receiver fixture does not prove live Salad GPU rendering, private Google Drive rclone bridge authorization, real YouTube OAuth or confirmed YouTube publish receipt. Test using non-production accounts and media after SSO and controlled credentials are provisioned.
4. **Runtime storage and network not provisioned.** Dedicated Linux service user, persistent owner directory, uploader vault, Scene users DB read-only access, SSO secret, host-only secure cookies, private filesystem permissions, Nginx reverse proxy and port collision review require explicit release approval.
5. **Final CI and wheel artifact must pass.** Only a successful commit-specific GitHub Actions build, module tests and wheel smoke at the final integration SHA can close this item. No local VPS production smoke tests were run.
6. **No deployment approval.** Do not merge this PR into main, publish image, mutate Scene SSO schema, open service ingress, deploy a new unit, or alter app.scenebuilder.online until blockers resolved separately.

## Exact offline acceptance

Install: python -m pip install -e '.[test]'
Run: python -m unittest discover -s tests -p 'test_reused_*.py' -v
Run: python -m unittest discover -s tests -p 'test_admin_security.py' -v
Run: python -m unittest app.modules.concat.test_routes -v
Run: python -m unittest discover -s app/modules/uploader/tests -p 'test_*.py' -v
Build: python -m pip wheel --no-deps --wheel-dir /tmp/concat-web-wheel .

## Production-safe release sequence (future only, NOT approved)

Complete upstream SSO issue #772 in a separately reviewed PR; validate on a disposable staging environment and read-only copy of account fixtures. Run guard CI and exact SHA acceptance, perform immutable backup, then request explicit merge and deployment authorization. Retain rollback and preserve existing Scene website and Drive Bridge. No live uploads are part of the current PR.
