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


## VPS read-only operator audit results — 2026-11-19 (08:03 PKT)

The operator pasted the following output from a **read-only VPS terminal audit**:

```text
=== NGINX ===
NGINX=PASS
=== EXISTING CONCAT ROUTE ===
CONCAT_ROUTE=EXISTS
=== DNS ===
137.74.162.143  STREAM concat.scenebuilder.online
137.74.162.143  DGRAM
137.74.162.143  RAW
=== TLS CERTIFICATES ===
/etc/letsencrypt/live/concat.scenebuilder.online/fullchain.pem
/etc/letsencrypt/live/app.scenebuilder.online/fullchain.pem
=== CONCAT APPLICATION ===
CONCAT_HEALTH=000
=== EXISTING WEBSITE ===
VPS1_HTTPS=303
CHANGES=NONE
```

**Verified facts and limits:**
- `nginx -t` passed.
- An active `nginx -T` server_name line contains `concat.scenebuilder.online`. It does **not** establish which config file, proxy target, or HTTP/HTTPS blocks are configured.
- DNS lookup resolved the Concat hostname to IPv4 `137.74.162.143`. This is resolver evidence, **not** proof that external HTTPS reaches the correct service.
- A Concat Let's Encrypt `fullchain.pem` pathname was found. This does **not** prove matching key, valid dates, hostname, trusted chain, or live TLS termination.
- Loopback `http://127.0.0.1:18873/healthz` returned curl HTTP `000` (no HTTP response; service unavailability vs connection details not yet proven). Do not claim Concat is deployed or healthy.
- Existing `https://app.scenebuilder.online/` returned HTTP `303`: a redirect, not an outage indicator. No changes were made.

### Release gate: public Nginx activation BLOCKED

The newly prepared `deploy/nginx-cutover.sh activate` intentionally requires the Concat virtual host to be *absent*. Because a Concat server_name **already exists**, it will intentionally **fail closed** with the existing-route conflict gate. **Do not invoke activate, overwrite the existing route, add a duplicate `server_name`, remove or rename an unverified Nginx file, request a replacement certificate, or reload Nginx** on the strength of the current audit.

**Next safe action:** Read-only inspection of the currently loaded Concat-related server block's file path, `listen`, `server_name`, `proxy_pass`, certificate path (not key contents), and any duplicate/other host ownership. Also inspect `systemctl show concat-web.service` load and active state, active Concat release symlink, local `ss -ltn` for port 18873, and actual cert validity/hostname. Never print private env, TLS private-key contents, passwords, authorization headers, OAuth tokens or entire `nginx -T` output.

After the evidence identifies who owns the existing Concat route, create a **separate guarded adoption path** for an existing Concat-only configuration if appropriate, preserving a byte-for-byte backup, syntax validation and reliable rollback. Do not treat the existing vhost as owned by the newly introduced script without that verification. Deploy the exact CI-passing integration SHA to loopback and verify health + anonymous denial **before** public cutover.

**Current status:** Nginx syntax passed and route/DNS/cert *paths* discovered; Concat application and public routing readiness still unverified. No VPS changes by this audit.


## Live Nginx block, service and certificate audit — 2026-11-19 (operator supplied)

The operator ran the read-only targeted inspection. Results:

```text
=== CONCAT NGINX CONFIGURATION ===
CONFIG_FILE= /etc/nginx/sites-enabled/concat-web.conf
DIRECTIVE= listen 80;
DIRECTIVE= server_name concat.scenebuilder.online;
CONFIG_FILE= /etc/nginx/sites-enabled/concat-web.conf
DIRECTIVE= listen 443 ssl;
DIRECTIVE= server_name concat.scenebuilder.online;
DIRECTIVE= ssl_certificate /etc/letsencrypt/live/concat.scenebuilder.online/fullchain.pem;
CONCAT_SERVER_BLOCKS= 2
=== CONCAT SERVICE ===
User=concatweb
LoadState=loaded
ActiveState=inactive
=== CURRENT RELEASE ===
CONCAT_RELEASE=NOT_SELECTED
=== PORT 18873 ===
[no listening sockets]
=== TLS CERTIFICATE ===
Hostname concat.scenebuilder.online does match certificate
CERT_VALID_NEXT_7_DAYS=YES
CHANGES=NONE
```

**Interpretation:** This VPS already has Concat-specific HTTP and HTTPS server blocks in `/etc/nginx/sites-enabled/concat-web.conf`, and a currently valid, hostname-matching Concat certificate (as checked by OpenSSL). The systemd unit is installed for `concatweb` but **inactive**; there is **no selected Concat release and no listener on 18873**. This explains the earlier loopback HTTP `000`. The existing Concat block audit did **not display any `proxy_pass`**, so whether the active HTTPS route forwards to 18873 remains **unverified**. Source-file includes, return/error directives, or a placeholder route could account for this.

**Updated release decision:** Prefer **reusing the existing Nginx Concat vhost** rather than creating a duplicate. The new-site-only `deploy/nginx-cutover.sh activate` must **NOT** be run because it deliberately rejects existing Concat configurations. Before any Nginx edit, inspect the exact owned site file's symlink destination, mode, directives, included snippets, and existing vhost's handling of `/healthz` (redacting credentials and key contents). Then decide whether the route is already suitable or whether to implement a *separate, guarded, byte-for-byte backed-up, Concat-only adoption/patch/rollback* path. Never overwrite unknown nginx content.

**Immediate dependency:** Stage the exact newly CI-verified integration SHA (which may change as audit docs are committed) with the existing Concat-only manual terminal helper, then confirm loopback `/healthz` and anonymous `/dashboard=401` before public route activation. Merely having a TLS vhost and certificate is not proof of a working Concat Web deployment.

No VPS changes were made by this read-only audit. Existing VPS1 website, Scene, Drive Bridge, Salad and other services remain outside Concat deployment scope.


## Additional live existing-route audit — 2026-11-19 (operator supplied)

```text
=== SITE OWNERSHIP ===
SITE_TARGET=/etc/nginx/sites-available/concat-web.conf
OWNER=root MODE=644
=== ROUTING DIRECTIVES ===
LOCATION= OTHER
LOCATION= /
RETURN_HTTP= 301
LOCATION= /
PROXY_DIRECTIVES= 0
INCLUDE_DIRECTIVES= 0
=== LOCAL HTTPS RESPONSE ===
CONCAT_HTTPS_HTTP=200
CHANGES=NONE
```

**Interpretation:** The enabled Concat route resolves to the root-owned, mode-0644 file `/etc/nginx/sites-available/concat-web.conf`. A line-oriented inspection of that exact file found two location blocks, a `301` return and **zero literal `proxy_pass` and `include` directives**. An HTTPS request using `--resolve concat.scenebuilder.online:443:127.0.0.1` to `/healthz` returned HTTP `200` even though the preceding live audit showed Concat service inactive, release unselected, and no listener on `18873`.

Consequently, this `200` **cannot be accepted as Concat application's `/healthz` proof**. The actual body, response provenance and other possible response directives (e.g. `return`, `root`, `try_files`, `alias`, `fastcgi_pass`) were not examined. Do not infer a live application from status alone. Do not alter the existing vhost until its behavior and file checksum are established.

**Nginx reuse strategy:** The source template's new-site-only `activate` operation is inapplicable to this existing site and must not be run. First stage the exact CI-passing integration SHA privately and verify its own loopback health and anonymous-denial. Separately audit the existing Concat vhost's safe routing directives and checksum, then implement guarded *existing-site adoption* only if needed: exact-site backup, hash-checked ownership, no symlinks outside known Concat site path, `nginx -t` and reload, HTTPS body/401 verification, Scene site's unchanged response, and restore-on-failure. The authorization to replace the existing vhost must be explicit, not assumed from an HTTP `200`.

No VPS changes were performed by this read-only command. No public Nginx reload, cutover, or Concat deployment has occurred as a result of this audit.


## Confirmed placeholder static-site identity — 2026-11-19

Operator ran a read-only audit of the existing enabled Concat Nginx site file
`/etc/nginx/sites-available/concat-web.conf`:

```text
=== CONFIG FINGERPRINT ===
SITE_SHA256=d0371b9ec8296a2e57dafafc2b7dda8268d70d23c0b657f5f46ea35e2c1819e5
=== RESPONSE CONFIGURATION ===
RETURN_COUNT=1
ROOT_COUNT=2
TRY_FILES_COUNT=2
=== HTTPS RESPONSE IDENTITY ===
HTTPS_STATUS=200
CONCAT_APP_HEALTH_BODY= NO
CHANGES=NONE
```

**Confirmed:** Existing Concat Nginx route is a static/placeholder response, **not the running application**; `/healthz` returned 200 but its JSON did not contain `{"service":"concat-web"}`. This is consistent with two `root`, two `try_files`, zero `proxy_pass` and zero `include` directives in the inspected Concat-only site. Actual Nginx syntax/contents were not displayed; do not assert more detail.

**Adoption safety anchor:** Existing Concat-only file's exact SHA-256 is
`d0371b9ec8296a2e57dafafc2b7dda8268d70d23c0b657f5f46ea35e2c1819e5`.
A future guarded adoption must match that hash, preserve a byte-for-byte private
backup, verify server_name and TLS material, and replace *only* the existing
Concat-specific file after the loopback service is healthy. Must refuse unknown
drift, duplicated server_name/other host conflicts, or a missing backup, and
roll back on `nginx -t`, reload, HTTPS response identity, anonymous denial or
existing Scene site status failures.

**No VPS config changes occurred.** No Concat service is running yet (as of
the preceding live service audit), and no live external Salad/Drive/YouTube
workflow has been proven.


## Implemented guarded adoption of existing Concat static placeholder — 2026-11-19

The exact static-site fingerprint above enabled a safer approach than full
replacement: `deploy/nginx-adopt-existing.py` now surgically changes **only**
the existing HTTPS `location /` block, using
`deploy/nginx-proxy-location.conf`. It does **not** replace HTTP or HTTPS
`server` blocks, remove ACME `/.well-known/` handling, change certificates,
modify other domains, create duplicate vhosts, or restart Scene/VPS1 services.
It refuses any site whose on-disk SHA256 differs from the audit's
`d0371b9ec8296a2e57dafafc2b7dda8268d70d23c0b657f5f46ea35e2c1819e5`
or whose ownership, enabled symlink, server names or location layout deviate.

### Prerequisites

1. The exact Integration commit must pass the full GitHub CI after these code
   changes and be staged by the Concat-only manual terminal release helper.
2. `/srv/concat-web/current` must select that exact SHA; `concat-web.service`
   must be active and loopback `/healthz` must identify `concat-web` with
   `ok=true`; anonymous `/dashboard=401`, login page `/auth/login=200`.
3. The original root-owned site must still match the pinned SHA256. Valid
   existing TLS certificate and the unchanged Scene HTTPS site must be checked.
4. Explicitly execute the integration release's code via the local VPS terminal,
   never from GitHub Actions.

**Manual read-only verification after Concat loopback stage:**

```bash
sudo python3 /srv/concat-web/current/deploy/nginx-adopt-existing.py audit
```

**Manual existing-route activation only after the preconditions pass:**

```bash
SHA=<EXACT_VERIFIED_INTEGRATION_SHA>
sudo python3 /srv/concat-web/current/deploy/nginx-adopt-existing.py activate "$SHA"
```

This makes a byte-for-byte, root-only backup in
`/var/backups/concat-web/nginx/original-<AUDITED_HASH>.conf` and a private
SHA/checksum marker in `/etc/concat-web/nginx-existing-adoption.json`.
It checks Nginx syntax, reloads Nginx (does **not** restart it), verifies
real HTTPS JSON identity, login/anonymous rejection and unchanged Scene
website status. On activation failure the code attempts to restore the
original static file and reload Nginx. If recovery also fails, it prints
a manual-recovery warning and retains the backup.

**Explicit rollback of a previously successful hash-verified adoption:**

```bash
sudo python3 /srv/concat-web/current/deploy/nginx-adopt-existing.py rollback "$SHA"
```

Rollback refuses out-of-band changes to the modified file and restores only
the backed-up Concat site; a pre-existing Nginx site is never deleted.

**Note:** The separate `deploy/nginx-cutover.sh activate` is for a *new*,
absent Concat vhost only. Never run it on this VPS because the Concat site
already exists. `deploy/manual-integration-release.sh` now includes
`deploy/` in the exact-SHA staged archive so the Nginx tools are actually
available on-host.

**Current coding/audit status:** Source and offline tests committed on
`integration/personal-release-20261119`; no VPS service or Nginx writes made
from this chat. Current Integration SHA must be re-verified after all commits.
