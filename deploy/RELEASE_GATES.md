# Concat Web: standalone admin, manual VPS terminal release gates

The old Scene SSO release requirement is permanently replaced by independent
single-admin authentication. **Concat Web never deploys through GitHub Actions.**

## Source and security acceptance (Steps 1–3)

1. Start only from a reviewed, CI-verified exact commit on `integration/personal-release-20261119`.
   **Do not merge into `main`** for this release. Compare GitHub branch SHA before manual release;
   never use an unreviewed feature branch or moving HEAD.
2. Verify a root-owned `0600` `/etc/concat-web/concat-web.env` contains
   `CONCAT_AUTH_MODE=local-admin`, single admin username, stdlib scrypt
   password hash, random independent Concat session and uploader secrets,
   HTTPS origins and strictly owner-scoped data storage. The host bootstrap
   `deploy/init-local-admin.py` must never print the password or hashes.
3. Verify private `/var/lib/concat-web`, uploader data, service user
   `concatweb` and root-owned release/helper. No Scene DB/issuer/key is read.
4. Run negative security tests: anonymous/forged-header/old Scene cookie denied,
   valid admin login, username/password failure and throttling, login CSRF,
   session CSRF/origin, revocation/logout, changed password invalidation,
   foreign/ownerless resource quarantine and no cross-app access.
5. Run actual standalone-login offline integration against checked MP4 receiver,
   copied Concat verified-source admission, persistent uploader queue,
   duplicate prevention and dashboard. This does **not** imply a live Salad
   GPU render or YouTube OAuth publishing was performed.
6. Run existing route/queue/dashboard tests, complete CI, wheel build and
   `bash -n deploy/guarded-release.sh`; ensure `deploy.yml` has no SSH,
   auto-deploy, or push-triggered production deployment.

## VPS terminal deployment, only after source acceptance (Step 4)

1. Read-only DNS/TLS/port/service/Nginx audit for
   `concat.scenebuilder.online` and port `18873`, preserve
   `app.scenebuilder.online`.
2. From the VPS terminal run `deploy/manual-integration-release.sh <exact-verified-integration-SHA> stage` from that same pinned GitHub SHA. The one-shot script verifies the branch and clone identity, takes private Concat-only state and helper backups, installs its root-owned new `deploy/guarded-release.sh`, invokes private admin bootstrap if necessary, and performs preflight + loopback staging. Do not reuse the earlier Scene-SSO-only helper.
3. Provision the independent private Concat admin environment **once**,
   interactively, through `deploy/init-local-admin.py`; never enter
   password or secrets in shell arguments or GitHub. Existing env is not
   overwritten. Back up prior independent Concat state.
4. Run `/usr/local/sbin/concat-web-deploy preflight <integration-SHA>` first.
   It must fail closed on missing or conflicting private identity/security.
5. Build exact-SHA archive from the verified integration branch and invoke the existing
   Concat-only helper in `deploy` mode. Install on `127.0.0.1:18873`,
   verify `/healthz`, and confirm anonymous `/dashboard` and
   `/api/v1/session` are denied. On health failure helper restores the
   previous Concat service only. Persistent owner/queue data is not reset.
6. Before public cutover, verify secure browser login/logout over HTTPS,
   CSRF, owner-scoped Concat Studio and Uploader, relevant GPU receiver tokens,
   Drive access, and controlled Salad render -> verified MP4 -> staging test.
   Real YouTube publication requires explicit separate approval.
7. Back up existing Nginx and certificate configuration; make only the
   isolated new Concat virtual-host change, validate `nginx -t`, HTTPS
   DNS and `app.scenebuilder.online` availability. On failure revert only
   Concat route/release. Keep Scene worker, LUTs, other VPS services,
   existing OAuth and canonical Drive Bridge untouched.

Historical Scene SSO blockers in `docs/FINAL_INTEGRATION_BLOCKERS_20261118.md`
apply to the superseded SSO design, not to this standalone admin branch.

**Current mode:** integration-only, manual VPS terminal. If local preflight, backup or loopback smoke fails, halt without modifying public Nginx. Do not claim real Salad or YouTube acceptance from CI fixtures.

The one-shot script intentionally **does not** alter public HTTPS/Nginx ingress; public activation still requires read-only host verification, backed-up Concat-only Nginx changes and real browser/GPU/Drive acceptance. No existing VPS1 service may be restarted as part of Concat staging.
