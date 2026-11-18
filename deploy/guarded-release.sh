#!/usr/bin/env bash
# Scoped, fail-closed release. Never edits VPS_1, nginx, Scene, Drive Bridge or Salad.
set -Eeuo pipefail
MODE="${1:-}"
SHA="${2:-}"
PACKAGE="${3:-}"
fail() { echo "Concat release gate: $*" >&2; exit 1; }
[[ "$(id -u)" -eq 0 ]] || fail "requires passwordless scoped sudo"
[[ "$MODE" == "preflight" || "$MODE" == "deploy" ]] || fail "invalid mode"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || fail "invalid SHA"
command -v flock >/dev/null || fail "flock is required"
exec 9>/run/lock/concat-web-deploy.lock
flock -n 9 || fail "another Concat release is active"

# Check-only infrastructure prerequisites. Bootstrap and SSO sign-off must occur
# separately; this script will never silently create auth grants or weaken security.
id concatweb >/dev/null 2>&1 || fail "dedicated concatweb user missing"
[[ -d /var/lib/concat-web ]] || fail "private data root missing"
runuser -u concatweb -- test -w /var/lib/concat-web || fail "concatweb cannot write its private data root"
[[ -d /srv/concat-web/releases ]] || fail "independent release directory missing"
[[ -f /etc/concat-web/concat-web.env ]] || fail "private environment file missing"
[[ "$(stat -c %a /etc/concat-web/concat-web.env)" == "600" ]] || fail "environment file must be mode 0600"
systemctl cat concat-web.service >/dev/null 2>&1 || fail "scoped service not installed"
systemctl cat concat-web.service | grep -Fq '/srv/concat-web/current/.venv/bin/python run.py' ||
  fail "unexpected service ExecStart; expected independent release virtualenv"
nginx -t >/dev/null 2>&1 || fail "existing nginx config invalid"

python3 - <<'PY'
import os, pathlib, sqlite3, subprocess, sys
p = pathlib.Path('/etc/concat-web/concat-web.env')
values = {}
for line in p.read_text().splitlines():
    if not line.strip() or line.lstrip().startswith('#'):
        continue
    if '=' not in line:
        sys.exit('Malformed env line')
    key, value = line.split('=', 1)
    values[key.strip()] = value.strip().strip('"').strip("'")
required = (
    'CONCAT_SCENE_USERS_DB', 'CONCAT_WEB_SECURITY_DB',
    'CONCAT_SCENE_SSO_SECRET', 'CONCAT_WEB_DATA_ROOT',
    'CONCAT_BOOTSTRAP_ADMINS', 'CONCAT_WEB_PUBLIC_ORIGIN',
    'CONCAT_SCENE_PUBLIC_ORIGIN', 'CONCAT_UPLOADER_ENABLED',
)
if any(not values.get(k) for k in required):
    sys.exit('Missing Concat environment value')
if len(values['CONCAT_SCENE_SSO_SECRET'].encode()) < 32:
    sys.exit('Weak Concat-only SSO signing key')
if values['CONCAT_WEB_PUBLIC_ORIGIN'] != 'https://concat.scenebuilder.online':
    sys.exit('Unapproved Concat origin')
if values['CONCAT_SCENE_PUBLIC_ORIGIN'] != 'https://app.scenebuilder.online':
    sys.exit('Unapproved Scene origin')
if values['CONCAT_UPLOADER_ENABLED'] != '1':
    sys.exit('Uploader must be explicitly enabled')
for key in ('CONCAT_WEB_SECURITY_DB', 'CONCAT_WEB_DATA_ROOT'):
    if not pathlib.Path(values[key]).resolve().is_relative_to('/var/lib/concat-web'):
        sys.exit('Concat storage path outside isolated data root')
db = values['CONCAT_SCENE_USERS_DB']
query = '''import sqlite3,sys,urllib.parse
p = urllib.parse.quote(sys.argv[1], safe='/')
c = sqlite3.connect('file:'+p+'?mode=ro',uri=True,timeout=3)
c.execute('PRAGMA query_only=ON')
c.execute('SELECT username,is_active,is_admin FROM users LIMIT 0')
c.execute('SELECT sid_hash,username_key,expires_at,revoked_at FROM concat_sso_sessions LIMIT 0')
'''
proc = subprocess.run(['runuser','-u','concatweb','--','python3','-c',query,db], capture_output=True, text=True)
if proc.returncode:
    sys.exit('Scene database read-only SSO verification failed; do not deploy')
PY

if [[ "$MODE" == "preflight" ]]; then
  echo "Concat independent runtime/SSO/schema preflight passed; no deployment performed"
  exit 0
fi

[[ "$PACKAGE" == "/tmp/concat-web-$SHA.tgz" ]] || fail "unexpected archive path"
[[ -f "$PACKAGE" && ! -L "$PACKAGE" ]] || fail "release archive missing"
RELEASE="/srv/concat-web/releases/$SHA"
[[ ! -e "$RELEASE" ]] || fail "immutable release SHA already exists"
ROOT="/srv/concat-web"
PREVIOUS=""
if [[ -L "$ROOT/current" ]]; then
  PREVIOUS="$(readlink -f "$ROOT/current")"
  [[ "$PREVIOUS" == "$ROOT/releases/"* ]] || fail "unsafe previous release symlink"
elif [[ -e "$ROOT/current" ]]; then
  fail "current path is not a release symlink"
fi
mkdir -m 0750 "$RELEASE"
tar --no-same-owner --no-same-permissions -xzf "$PACKAGE" -C "$RELEASE"
chown -R concatweb:concatweb "$RELEASE"
runuser -u concatweb -- python3 -m venv "$RELEASE/.venv"
runuser -u concatweb -- "$RELEASE/.venv/bin/python" -m pip install --no-input --disable-pip-version-check "$RELEASE"
rm -f "$PACKAGE"
ln -s "$RELEASE" "$ROOT/.new-$SHA"
mv -Tf "$ROOT/.new-$SHA" "$ROOT/current"
rollback() {
  echo "Concat-only health check failed; restoring previous Concat release" >&2
  if [[ -n "$PREVIOUS" ]]; then
    ln -s "$PREVIOUS" "$ROOT/.rollback-$SHA"
    mv -Tf "$ROOT/.rollback-$SHA" "$ROOT/current"
    systemctl restart concat-web.service || :
  else
    systemctl stop concat-web.service || :
    rm -f "$ROOT/current"
  fi
}
if ! systemctl restart concat-web.service; then rollback; fail "service start failed"; fi
healthy=0
for i in {1..10}; do
  if curl --fail --silent --show-error --max-time 3 http://127.0.0.1:18873/healthz |
      grep -Fq '"service":"concat-web"'; then healthy=1; break; fi
  sleep 2
done
if [[ "$healthy" -ne 1 ]] ||
   ! curl --fail --silent --show-error --max-time 10 https://concat.scenebuilder.online/healthz |
      grep -Fq '"service":"concat-web"'; then
  rollback
  fail "local or public health check failed"
fi
# Verify anonymous access is denied on both loopback and the public HTTPS route.
# Never treat a public health endpoint as evidence of authenticated safety.
local_status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 6 http://127.0.0.1:18873/api/v1/session || true)"
public_status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 https://concat.scenebuilder.online/api/v1/session || true)"
if [[ "$local_status" != "401" || "$public_status" != "401" ]]; then
  rollback
  fail "anonymous session API does not fail closed"
fi
echo "Concat-only release $SHA healthy; VPS_1 and nginx unchanged"
