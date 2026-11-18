# Concat Web Scene SSO and Security Contract (Agent 5)

Status: **Concat consumer implemented; upstream Scene issuer NOT present**. Separate upstream issue: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/issues/772. Do not merge/deploy or open ingress until approved end-to-end integration and negative tests.

## Provenance and ownership

Reuses VPS_1 Scene's `users` table semantics (`username COLLATE NOCASE`, `is_active`, `is_admin`) and existing role revalidation per request. Scene remains the sole authority for accounts, passwords and Scene admin role. Concat alone stores app-specific grants, session revocations and non-secret settings in a separate SQLite file. No user, admin or worker can claim ownerless legacy job/media rows, even in an administrator's ordinary Concat context.

Existing Scene uses Starlette's host-only, signed session cookie and does **not** currently have a cross-subdomain assertion endpoint or `concat_sso_sessions` schema. There is intentionally no fallback to a request header, Scene raw cookie, account password, magic admin, or `__default__`.

## Required environment before starting on a secured host

```env
CONCAT_WEB_PUBLIC_ORIGIN=https://concat.scenebuilder.online
CONCAT_SCENE_PUBLIC_ORIGIN=https://app.scenebuilder.online
CONCAT_SCENE_USERS_DB=/opt/scene-builder-web/data/web.sqlite3
CONCAT_WEB_SECURITY_DB=/var/lib/concat-web/security.sqlite3
CONCAT_SCENE_SSO_SECRET=<separate-32+-byte-secret-shared-with-Scene-issuer>
CONCAT_BOOTSTRAP_ADMINS=<explicit-canonical-existing-Scene-admin-username>
```

Provision the Concat SQLite directory separately with restrictive permissions. Grant the Concat service read-only access to the Scene database (including SQLite journal/WAL access); never write or migrate Scene's database from Concat. `CONCAT_BOOTSTRAP_ADMINS` grants initial access only when the Scene account is verified active and currently admin; an explicit denied grant supersedes bootstrap. Do not commit environment values, SQLite, tokens, or passwords. **Never reuse `SESSION_SECRET` or `WEB_ACCESS_KEY` as the SSO key.**

## Browser flow

1. `GET /auth/scene/start` generates short-lived, Secure, HttpOnly, SameSite=Lax, host-only state and sends the browser to Scene `GET /concat-sso/authorize` with pinned `client_id=concat-web` and `redirect_uri=https://concat.scenebuilder.online/auth/scene/callback`. Scene's existing login may resume this safe path via `/login?next=...`.
2. Scene issuer must require an active logged-in Scene account and its own revocable server-side session ID. It creates signed ticket with base64url JSON, a dot and HMAC-SHA256 of `scene-concat-v1.<encoded-json>` using `CONCAT_SCENE_SSO_SECRET`. Claims: `v=1`, `iss=https://app.scenebuilder.online`, `aud=concat-web`, `sub=canonical-username`, `sid=random-url-safe-session-id`, `state=exact-state`, `jti=random-url-safe-unique-id`, integer `iat`, integer `exp` max 90s; recommended actual TTL 60s. SID/JTI 32–128 URL-safe characters.
3. Callback validates signed claims, nonce, ticket age, exact issuer/audience, active Scene user, Scene revocable session row, Concat grant, and one-time JTI before minting a separate Secure, HttpOnly, host-only Concat session. Database transaction prevents ticket replay. Callback never accepts user-provided role/grant.
4. Every authenticated Concat request reads its *server-side* local session and checks current Scene `users`, Scene `concat_sso_sessions` and Concat grant. Scene logout, disable/delete, role demotion, app grant revocation and admin-initiated session revocation apply at the next request.
5. `GET /api/v1/auth/csrf` returns a session-bound token; all Concat browser mutations require that value in `X-CSRF-Token` **and** an exact same-origin `Origin` or `Referer`. `POST /auth/logout` revokes the Concat session. Cookies are Secure, HttpOnly, SameSite=Lax, host-only with `__Host-` prefix.

Upstream Scene must add **readable** `concat_sso_sessions(sid_hash TEXT PRIMARY KEY, username_key TEXT, expires_at INTEGER, revoked_at INTEGER NULL)` and invalidate its rows on Scene logout/expiry. Without this schema/issuer, login is deliberately rejected. Inactive Scene accounts and missing/malformed schema fail closed.

## Admin / owner rules

- `/admin` and `/api/v1/admin/**`: active verified Scene admin with explicit Concat grant; Scene roles cannot be edited from Concat.
- Admin can list Scene account status, grant/revoke **Concat-only** access and revoke Concat sessions. Config only exposes maintenance mode and bounded session lifetime. Password hash, OAuth tokens, service secrets, GPU keys, video filesystem paths and private credentials are never returned.
- Agent 2/3/4 must use `current_principal` and `require_owned_resource(principal, row, app)` for each individual job/media/channel/credential/queue/Sheet/thumbnail action. `app` must be literal `concat` or `concat-uploader` (or `dashboard` for dashboard-only persisted rows); record `owner_username` must be immutable, canonical and non-null. Always enforce `WHERE id=? AND owner_username=? AND app=?` atomically in the actual persistence layer, including mutations, callbacks, links, history and queue claims. The Python helper is defense in depth, **not a substitute for scoped SQL and worker authorization**.
- Admin is NEVER exempt from owner checks. Ownerless, `__default__`, legacy, foreign and malformed records are invisible (404), not automatically attributed. Copied Cloud Render's original admin/global tables remain unsafe until Agent 2 migrates or quarantines them and negative integration tests pass.
- Reject inbound `X-Auth-User`, `X-Is-Admin`, `X-App-Access` identity inputs; do not mount untrusted direct legacy upstream ports. OAuth client sharing is separate from user token isolation.

## Verification and release gate

`python -m unittest discover -s tests -p 'test_admin_security.py' -v`, plus baseline `test_foundation.py`, run in a dedicated Admin PR workflow. Fixtures use only temporary SQLite + signed test tickets; no production data. Agent 2/3 must run genuine two-owner negative HTTP, database, worker, media, Drive, uploader and cross-app tests after their adapters are wired. Upstream issue #772 is a **hard end-to-end SSO dependency**. A passing isolated consumer test is not a claim that live Scene issuer, integrated media routes, or deployed ownership are verified. Keep server ingress closed, no merge/deployment.
