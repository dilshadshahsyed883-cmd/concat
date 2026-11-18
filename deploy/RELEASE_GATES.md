# Independent Concat Web release gates (public cutover NOT yet authorized)

1. Confirm DNS and TLS for proposed `concat.scenebuilder.online` via separate read-only discovery.
2. Inventory exact VPS_1 ports, Nginx locations, Linux users and production services; check collisions with 18873.
3. Verify Agent 5 Scene-session SSO: secure cookies or server-verified auth_request, role refresh, session revocation and anti-forgery. Never trust arbitrary inbound identity headers.
4. Verify owner-bound Cloud Render media/job/config/ingest callbacks and quarantine unknown legacy owners (audit PR #768).
5. Verify owner+app uploader queues, channels, Sheets/OAuth tokens and canonical Drive-backed media handoff (PR #770 contract). Preserve Copy Video Link URL and Drive Bridge canonical identity/verification/cleanup.
6. Test Alice, Bob and admin (admin as own owner) on all API reads/writes, copied URLs, direct guessed IDs and worker callbacks.
7. Validate complete suite, real provider smoke in controlled mode and effective TLS/NGINX config without touching current routes.
8. Snapshot independent release and persistent state (consistent SQLite backup where appropriate). Keep previous working service and Nginx config.
9. Use the manually approved, SHA-pinned `deploy.yml` workflow from protected `main` to stage the independent Concat unit **on loopback only**; keep existing public Nginx placeholder/routes unchanged. Never deploy from feature branches. Do not alter the host-installed Drive Bridge.
10. Before public activation: verify live Scene issuer deployment/schema, same independent HMAC key, ticket URL logging redaction, real two-user/admin browser SSO + revocation and controlled GPU → verified MP4 → Uploader acceptance against the staged unit. Only then authorize a separate, backed-up Concat-only Nginx cutover. Afterwards test public /healthz, authenticated UI, owner isolation, Nginx -t and existing app.scenebuilder.online. On failure roll back only the Concat unit/release/route. Preserve queues and data.
Integration PR #7 and upstream issuer PR #773 are merged in source, but actual Scene issuer deployment and cross-app signing/access remain unverified. Production public activation remains gated; historical release blockers are documented in docs/FINAL_INTEGRATION_BLOCKERS_20261118.md. Do not treat isolated tests or a loopback 200 as live SSO/Salad/YouTube acceptance.
