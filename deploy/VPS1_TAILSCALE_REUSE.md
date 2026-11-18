# Concat Web — reuse VPS_1 Tailscale/SSH deployment (2026-11-18)

## Architecture

`concat-web/main` → **focused CI and security tests** → existing VPS_1
`tailscale/github-action@v4` OIDC identity (`tag:ci`) → existing
`vps1deploy` SSH key and **pinned** Tailscale host key → a **new separate
root-owned** `/usr/local/sbin/concat-web-deploy` helper → private
`concat-web.service` on `127.0.0.1:18873` → Concat-only health/401 checks
and rollback. No Concat deploy uses the VPS_1 production deploy helper, changes
Nginx, or restarts Scene, Drive Bridge, Salad, or OAuth.

This deliberately reuses the **transport/identity**, NOT existing VPS_1
application state, databases, release files, deploy command, or rollback.

## GitHub environment (one-time)

1. Create or inspect `concat-web-production` **in the concat-web repository**
   and restrict it to protected `main`. Keep `main` branch protected.
2. Provision these three **environment secrets** there (names match VPS_1):
   - `TS_OAUTH_CLIENT_ID` — existing approved Tailscale OIDC client ID
   - `TS_AUDIENCE` — existing Tailscale OIDC audience
   - `VPS1_SSH_PRIVATE_KEY` — existing verified non-root VPS1 SSH identity;
     runner checks the hard-pinned expected public-key fingerprint
3. Secrets in `vps1-deploy-control` are **not** automatically shared with
   `concat-web`. Re-enter them securely in GitHub; never put keys in an
   issue, pull request, chat, commit, artifact or log. The connector cannot
   read/reuse those secret values.
4. Verify the **Tailscale OAuth/OIDC policy explicitly authorizes the
   `concat-web` repository and `tag:ci`** for the existing tailnet. VPS1
   authorization for its own repository does not prove this access.
5. Keep these repository variables **unset/false** until the host + Scene SSO
   preflight passes:
   - `CONCAT_WEB_DEPLOY_ENABLED=true` — allows manual `deploy`.
   - `CONCAT_WEB_AUTO_DEPLOY_ENABLED=true` — additionally enables authorized
     `main` push deployment, but *only after tests pass*.
   A manual `preflight` is allowed without either variable.

### Pinned identity

Workflow reuses the verified VPS_1 `vps1deploy` user and Tailscale SSH host
key, not public inbound SSH. The pinned known-host entry/fingerprint were
copied from the current VPS1 production workflow; fail closed if rotated.
No `ssh-keyscan`, identity header trust or `StrictHostKeyChecking=no`.

## One-time host preparation (VPS operator only, no Scene restart)

The VPS_1 `vps1-production-deploy` helper is **not** modified. Run only after
inspecting the existing VPS host; use the reviewed source from
`concat-web/main`, never arbitrary branch code.

- Confirm `127.0.0.1:18873` is unused and that current Nginx/Scene services
  are healthy; snapshot the existing Concat placeholder config.
- Set up dedicated `concatweb` system user and state:
  `/var/lib/concat-web` (0700, owned by concatweb),
  `/var/lib/concat-web/uploader` (0700, owned by concatweb),
  `/srv/concat-web/releases` (0755, root-owned),
  `/etc/concat-web` (0700, root-owned).
- Install `deploy/concat-web.service` as
  `/etc/systemd/system/concat-web.service`. Keep it stopped before first
  release; run `systemctl daemon-reload`. Only the Concat unit is managed.
- Install the *reviewed* `deploy/guarded-release.sh` as root-owned
  `/usr/local/sbin/concat-web-deploy` (0750). It **must not** be a symlink.
  The release runner calls only this installed executable. No
  `sudo bash -s` or execution of uploaded scripts as root.
- Configure a dedicated sudoers entry, syntax-checked with `visudo -cf`,
  permitting the existing non-root `vps1deploy` identity to execute
  **only** `/usr/local/sbin/concat-web-deploy` without a password. Never
  grant `sudo ALL`, direct shell, systemctl, tar or Nginx access to that
  account. Host helper independently validates mode, SHA, manifest digest,
  release paths, user and service.
- Place `/etc/concat-web/concat-web.env` as root-owned mode 0600, based
  on `.env.example`. Supply independently generated, **different** long
  `CONCAT_SCENE_SSO_SECRET` and `LUM_SESSION_SECRET`. SSO secret must match
  the Scene issuer's separately provisioned setting; never use Scene
  `SESSION_SECRET` or an OAuth token.
- Live Scene issuer from VPS1 PR #773 **must actually be deployed**, with
  `concat_sso_sessions` initialized. Grant `concatweb` the minimum
  read-only access to the **live** Scene SQLite `users` and
  `concat_sso_sessions` tables and any necessary WAL visibility. Concat
  must never get Scene DB write access, and Scene must retain ownership of
  its database. Review directory traversal and ACLs individually.
- Install Python 3.12, `python3-venv`, Python pip, OpenSSH client/server,
  `flock`, `nginx`, `curl`. Verify host-installed Nginx config with
  `nginx -t` but **do not** activate Concat proxy yet.

Example one-time setup (after checking no existing conflicting paths/users):

```bash
sudo id concatweb >/dev/null 2>&1 || sudo useradd --system --home /var/lib/concat-web --shell /usr/sbin/nologin concatweb
sudo install -d -o concatweb -g concatweb -m 0700 /var/lib/concat-web /var/lib/concat-web/uploader
sudo install -d -o root -g root -m 0755 /srv/concat-web /srv/concat-web/releases
sudo install -d -o root -g root -m 0700 /etc/concat-web
sudo install -o root -g root -m 0750 deploy/guarded-release.sh /usr/local/sbin/concat-web-deploy
sudo install -o root -g root -m 0644 deploy/concat-web.service /etc/systemd/system/concat-web.service
sudo systemctl daemon-reload
```

Configure the root-only env file and narrow sudoers **separately** after this
setup; do not copy template secrets literally.

## Execution and acceptance

- **Preflight:** GitHub Actions → Concat Web guarded Tailscale release →
  Run workflow on `main`, action `preflight`, enter exact `main` SHA.
  This runs all tests, joins the tailnet, verifies pinned SSH key and
  root-owned host helper, checks the live SSO schema as `concatweb`, and
  stops **without deploying**.
- **First release:** enable `CONCAT_WEB_DEPLOY_ENABLED` only after preflight
  and controlled operator review. Dispatch `deploy` with exact SHA. The
  helper verifies SHA-256 of the uploaded archive, extracts only safe
  files, installs an isolated Python environment and atomically selects the
  release. On failure it restores the previous Concat symlink/unit.
  `/var/lib/concat-web` persists; VPS1/Scene services and Nginx never change.
- **Auto deploy:** only when both variables are true, authorized
  `main` pushes perform **the same CI + host checks before** Concat-only
  loopback deployment. Set variable false for a global kill switch.
  Note this auto deploy is **not** automatic public cutover.
- **Public cutover remains separate:** after two-user/admin HTTPS SSO,
  CSRF, role/grant revocation, ownerless quarantine, and controlled
  Salad → verified MP4 → Concat Uploader acceptance, manually review a
  *Concat-only* Nginx/TLS change, including ticket/state query-log
  redaction, back up its config, run `nginx -t`, and preserve
  `app.scenebuilder.online` unmodified.

**Fail-closed limitations:** GitHub merging files cannot provision secrets,
Tailscale OAuth policies, root-owned service/helper or the live Scene SSO
schema. This setup document does not claim VPS access or a successful live
deployment.