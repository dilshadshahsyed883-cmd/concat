# Concat Web Nginx HTTPS routing — 2026-11-19

**Status:** GitHub source prepared for review; **VPS routing NOT activated**.
**Source branch:** `integration/personal-release-20261119` (NOT `main`).
**PR:** https://github.com/dilshadshahsyed883-cmd/concat-web/pull/14

## Scope and files

- `deploy/nginx-site.example.conf`: isolated port 80 redirect + HTTPS/443
  virtual host for **only** `concat.scenebuilder.online`, reverse proxy to
  `127.0.0.1:18873`; no Scene SSO or trusted client identity header.
- `deploy/nginx-cutover.sh`: explicit `audit`, `activate`, `rollback` operations.
  Audit is read-only; activation requires an exact deployed integration SHA,
  active Concat-only service, healthy loopback endpoint, anonymous access
  denial, valid TLS certificate for the hostname (more than 7 days remaining),
  matching private key, available dedicated site paths, and valid existing
  Nginx config.
- `tests/test_nginx_routing.py`: fail-closed Nginx configuration and
  rollback-contract tests, run by CI. Tests do not inspect VPS or contact
  the real Salad/YouTube services.

The site template disables Nginx access logging to avoid leaking query-string
OAuth credentials, strips browser-provided identity headers, streams potentially
large uploads, sets HSTS and other common security headers, and does not
rewrite or modify any existing `app.scenebuilder.online` server block.

The cutover script only creates:
- `/etc/nginx/sites-available/concat-web.conf`
- `/etc/nginx/sites-enabled/concat-web.conf` (symlink)
- `/etc/concat-web/nginx-site-managed` (root-only SHA/checksum marker)

It tests `nginx -t` before and after; only **reloads Nginx**, never restarts
VPS1/Scene/Concat services. If activation fails it removes the newly created
Concat site, validates config and reloads again. An explicit rollback removes
only the checksum-verified Concat-managed site, never other virtual hosts.
If existing paths, route or drift conflict, it fails without overwriting them.

## Live VPS prerequisites still unknown

**Do not claim these as passing until operator audit reports them:**

1. `concat.scenebuilder.online` public DNS A/AAAA/Cloudflare origin relationship
2. Existing Nginx includes `/etc/nginx/sites-enabled/*` and has no conflicting
   Concat server-name block (not to be overwritten)
3. Trusted certificate/fullchain and matching private key available on VPS
   **for this exact hostname**; cert issuance is outside this script
4. Concat service is deployed from an **exact, CI-verified integration SHA**
   at `/srv/concat-web/releases/<SHA>`, bound only to `127.0.0.1:18873`;
   `/healthz` works and `/dashboard` denies anonymous access
5. Current `app.scenebuilder.online` is healthy and remains unchanged after cutover

## Read-only VPS terminal audit (safe to run before deployment)

```bash
sudo bash <<'SH'
set -Eeuo pipefail
echo '=== CONCAT NGINX READ-ONLY AUDIT ==='
if nginx -t >/dev/null 2>&1; then echo NGINX_SYNTAX=PASS; else echo NGINX_SYNTAX=FAIL; fi
if nginx -T 2>/dev/null | grep -E '^[[:space:]]*include[[:space:]]+/etc/nginx/sites-enabled/\*;' >/dev/null; then
  echo SITES_ENABLED_INCLUDE=PRESENT
else
  echo SITES_ENABLED_INCLUDE=NOT_CONFIRMED
fi
if nginx -T 2>/dev/null | grep -E '^[[:space:]]*server_name[[:space:]]+concat\.scenebuilder\.online([[:space:];]|$)' >/dev/null; then
  echo CONCAT_SERVER_NAME=ALREADY_CONFIGURED
else
  echo CONCAT_SERVER_NAME=NOT_CONFIGURED
fi
for path in /etc/nginx/sites-available/concat-web.conf /etc/nginx/sites-enabled/concat-web.conf; do
  if test -e "$path" || test -L "$path"; then echo "CONCAT_PATH_PRESENT=$path"; fi
done
getent ahostsv4 concat.scenebuilder.online >/dev/null 2>&1 && echo DNS_A=RESOLVES || echo DNS_A=UNKNOWN
getent ahostsv6 concat.scenebuilder.online >/dev/null 2>&1 && echo DNS_AAAA=RESOLVES || echo DNS_AAAA=NONE_OR_UNKNOWN
for endpoint in http://127.0.0.1:18873/healthz http://127.0.0.1:18873/dashboard https://app.scenebuilder.online/; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "$endpoint" 2>/dev/null || true)"
  case "$endpoint" in
    */healthz) echo "CONCAT_LOOPBACK_HEALTH_HTTP=$code";;
    */dashboard) echo "CONCAT_ANONYMOUS_HTTP=$code";;
    *) echo "EXISTING_SCENE_HTTPS_HTTP=$code";;
  esac
done
if test -d /etc/letsencrypt/live; then
  find /etc/letsencrypt/live -maxdepth 2 -name fullchain.pem -print
else
  echo LETSENCRYPT_LIVE_DIR=ABSENT
fi
echo CHANGES=NONE
SH
```

The grep checks intentionally consume complete Nginx output under pipefail, avoiding premature SIGPIPE false negatives.\n\nThis audit deliberately does **not** print private environment files, login
credentials, TLS private-key contents, full Nginx configs, or OAuth tokens.
It does not install packages, request a certificate, modify routes or reload
services. Paste its sanitized output into the same PR audit trail.

## Manual activation only AFTER a successful guarded Concat loopback stage

With an exact successful, reviewed integration SHA (`SHA`) and known
certificate/key paths verified on VPS, run the **pinned release's own script**:

```bash
sudo bash /srv/concat-web/current/deploy/nginx-cutover.sh audit
sudo bash /srv/concat-web/current/deploy/nginx-cutover.sh activate \
  "$SHA" /etc/letsencrypt/live/concat.scenebuilder.online/fullchain.pem \
  /etc/letsencrypt/live/concat.scenebuilder.online/privkey.pem
```

The paths above are **examples**, not evidence that those files exist.
Use the actual verified matching certificate and key; other legitimate
certificate paths may be used. The guard requires a trusted certificate
and direct HTTPS health/login checks. It does not issue certificates.

If the Concat-managed new site must be undone:

```bash
sudo bash /srv/concat-web/current/deploy/nginx-cutover.sh rollback "$SHA"
```

No public Nginx cutover was performed while writing this document or CI.
Real browser login, Salad GPU, Drive Bridge and Youtube OAuth acceptance
remain independent operational verification tasks.
