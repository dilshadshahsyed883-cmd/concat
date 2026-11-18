# Independent Concat Web release gates (NOT authorized to deploy)

1. Confirm DNS and TLS for proposed `concat.scenebuilder.online` via separate read-only discovery.
2. Inventory exact VPS_1 ports, Nginx locations, Linux users and production services; check collisions with 18873.
3. Verify Agent 5 Scene-session SSO: secure cookies or server-verified auth_request, role refresh, session revocation and anti-forgery. Never trust arbitrary inbound identity headers.
4. Verify owner-bound Cloud Render media/job/config/ingest callbacks and quarantine unknown legacy owners (audit PR #768).
5. Verify owner+app uploader queues, channels, Sheets/OAuth tokens and canonical Drive-backed media handoff (PR #770 contract). Preserve Copy Video Link URL and Drive Bridge canonical identity/verification/cleanup.
6. Test Alice, Bob and admin (admin as own owner) on all API reads/writes, copied URLs, direct guessed IDs and worker callbacks.
7. Validate complete suite, real provider smoke in controlled mode and effective TLS/NGINX config without touching current routes.
8. Snapshot independent release and persistent state (consistent SQLite backup where appropriate). Keep previous working service and Nginx config.
9. Deploy only from reviewed protected main with explicit approval, SHA pinning, serial lock and host-installed Drive Bridge guarded process. Never deploy from feature branches.
10. Post-activation: check /healthz, authenticated HTML, owner isolation, Nginx -t and existing app.scenebuilder.online. On failure roll back only concat-web unit/release/route. Preserve data.
Integration now contains copied working modules, owner-specific runtime adapters and offline smoke tests. Production remains unauthorized; upstream Scene SSO issuer #772 and real external-service acceptance are documented in docs/FINAL_INTEGRATION_BLOCKERS_20261118.md. Do not treat isolated tests as live SSO/Salad/YouTube acceptance.
