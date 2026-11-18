#!/usr/bin/env bash
# Root-owned, Concat-only deploy helper installed at /usr/local/sbin/concat-web-deploy.
# Never changes Scene, Nginx, Drive Bridge, Salad, OAuth, or VPS1 production services.
set -Eeuo pipefail
umask 077
MODE="${1:-}"
SHA="${2:-}"
PACKAGE="${3:-}"
ARCHIVE_DIGEST="${4:-}"
fail() { echo "Concat release gate: $*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || fail "restricted sudo is required"
[[ "$MODE" == "preflight" || "$MODE" == "deploy" ]] || fail "invalid operation"
[[ "$SHA" =~ ^[a-f0-9]{40}$ ]] || fail "invalid release SHA"
[[ "$(readlink -f -- "$0")" == "/usr/local/sbin/concat-web-deploy" ]] ||
  fail "host-installed helper required"
[[ ! -L /usr/local/sbin/concat-web-deploy ]] ||
  fail "host helper must not be symlinked"
[[ "$(stat -c %U /usr/local/sbin/concat-web-deploy)" == root ]] ||
  fail "host helper is not root-owned"
[[ -z "$(find /usr/local/sbin/concat-web-deploy -maxdepth 0 -perm /022 -print -quit)" ]] ||
  fail "host helper writable by non-root"
exec 9>/run/lock/concat-web-deploy.lock
flock -n 9 || fail "another Concat release is in progress"

# No remote SSH command may write to VPS1/Scene locations.
id concatweb >/dev/null || fail "dedicated concatweb user missing"
id vps1deploy >/dev/null || fail "existing VPS1 deploy identity missing"
[[ -d /var/lib/concat-web ]] || fail "Concat owner storage missing"
runuser -u concatweb -- test -w /var/lib/concat-web ||
  fail "Concat cannot write its own persistent state"
[[ -d /srv/concat-web/releases ]] || fail "Concat releases path missing"
[[ "$(stat -c %U /srv/concat-web/releases)" == root ]] ||
  fail "releases directory not root-owned"
[[ -f /etc/concat-web/concat-web.env && ! -L /etc/concat-web/concat-web.env ]] ||
  fail "private Concat environment missing"
[[ "$(stat -c %U /etc/concat-web/concat-web.env)" == root ]] ||
  fail "private environment not root-owned"
[[ "$(stat -c %a /etc/concat-web/concat-web.env)" == "600" ]] ||
  fail "private environment must be mode 600"
systemctl cat concat-web.service >/dev/null 2>&1 ||
  fail "independent Concat unit missing"
systemctl cat concat-web.service |
  grep -Fq '/srv/concat-web/current/.venv/bin/python run.py' ||
  fail "unexpected Concat service executable"
[[ "$(systemctl show -P User concat-web.service)" == concatweb ]] ||
  fail "Concat service user mismatch"
nginx -t >/dev/null 2>&1 || fail "existing nginx config invalid"

# Verify the *live* Scene DB from the Concat service identity, not a copied
# ownerless fixture. SQLite is opened read-only; no migration or write here.
python3 - <<'PY'
import pathlib, sqlite3, subprocess, sys
p = pathlib.Path('/etc/concat-web/concat-web.env')
values = {}
for line in p.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    if '=' not in line:
        sys.exit('Invalid environment line')
    key, value = line.split('=', 1)
    values[key.strip()] = value.strip().strip('"').strip("'")
required = (
    'CONCAT_SCENE_USERS_DB', 'CONCAT_WEB_SECURITY_DB',
    'CONCAT_SCENE_SSO_SECRET', 'CONCAT_WEB_DATA_ROOT',
    'CONCAT_WEB_PUBLIC_ORIGIN', 'CONCAT_SCENE_PUBLIC_ORIGIN',
    'CONCAT_UPLOADER_ENABLED', 'LUM_SESSION_SECRET',
    'LUM_DATA_DIR', 'CONCAT_CLOUD_RENDER_ROOT',
    'CONCAT_WEB_HOST', 'CONCAT_WEB_PORT',
)
if any(not values.get(k) for k in required):
    sys.exit('Missing independent Concat environment setting')
if len(values['CONCAT_SCENE_SSO_SECRET'].encode()) < 32 or len(values['LUM_SESSION_SECRET'].encode()) < 32:
    sys.exit('Missing strong SSO/uploader secrets')
if values['CONCAT_SCENE_SSO_SECRET'] == values['LUM_SESSION_SECRET']:
    sys.exit('SSO and uploader secrets must be distinct')
exact = {
    'CONCAT_WEB_PUBLIC_ORIGIN': 'https://concat.scenebuilder.online',
    'CONCAT_SCENE_PUBLIC_ORIGIN': 'https://app.scenebuilder.online',
    'CONCAT_WEB_HOST': '127.0.0.1',
    'CONCAT_WEB_PORT': '18873',
    'CONCAT_WEB_DATA_ROOT': '/var/lib/concat-web',
    'CONCAT_WEB_SECURITY_DB': '/var/lib/concat-web/security.sqlite3',
    'CONCAT_UPLOADER_ENABLED': '1',
    'LUM_DATA_DIR': '/var/lib/concat-web/uploader',
    'CONCAT_CLOUD_RENDER_ROOT': '/var/lib/concat-web',
}
if any(values.get(k) != v for k, v in exact.items()):
    sys.exit('Unexpected Concat isolation or HTTPS config')
scene = pathlib.Path(values['CONCAT_SCENE_USERS_DB']).resolve()
if not scene.is_file() or scene == pathlib.Path(values['CONCAT_WEB_SECURITY_DB']).resolve():
    sys.exit('Scene account database missing or unsafe')
query = '''import sqlite3,sys
from urllib.parse import quote
p = quote(sys.argv[1], safe='/')
c = sqlite3.connect("file:" + p + "?mode=ro", uri=True, timeout=5)
c.execute('PRAGMA query_only=ON')
c.execute('SELECT username,is_active,is_admin FROM users LIMIT 0')
c.execute('SELECT sid_hash,username_key,expires_at,revoked_at FROM concat_sso_sessions LIMIT 0')
'''
p = subprocess.run(['runuser', '-u', 'concatweb', '--', 'python3', '-c', query, str(scene)], capture_output=True, text=True)
if p.returncode:
    sys.exit('Concat cannot read live Scene users + revocable sessions read-only')
print('Concat SSO SQLite read-only readiness: PASS')
PY
if [[ "$MODE" == preflight ]]; then
  echo "PASS: private tailnet host, isolated service and Scene SSO read-only checks; NO deployment"
  exit 0
fi

[[ "$PACKAGE" == "/tmp/concat-web-$SHA.tgz" ]] ||
  fail "unexpected uploaded archive path"
[[ "$ARCHIVE_DIGEST" =~ ^[a-f0-9]{64}$ ]] || fail "missing archive SHA-256"
[[ -f "$PACKAGE" && ! -L "$PACKAGE" ]] || fail "missing regular archive"
[[ "$(stat -c %U "$PACKAGE")" == vps1deploy ]] || fail "unexpected archive owner"
[[ "$(stat -c %h "$PACKAGE")" == 1 ]] || fail "linked archive rejected"
[[ ! -e "/srv/concat-web/releases/$SHA" ]] || fail "immutable release SHA already staged"

ROOT="/srv/concat-web"
RELEASE="$ROOT/releases/$SHA"
PREVIOUS=""
if [[ -L "$ROOT/current" ]]; then
  PREVIOUS="$(readlink -f "$ROOT/current")"
  [[ "$PREVIOUS" == "$ROOT/releases/"* ]] || fail "unexpected previous release"
elif [[ -e "$ROOT/current" ]]; then
  fail "current release must be a symlink"
fi

# Copy untrusted SSH upload into root-owned private staging before validating.
STAGE="$(mktemp -d "$ROOT/releases/.staging.XXXXXXXX")"
SWITCHED=0
cleanup_and_rollback() {
  result=$?
  trap - EXIT
  if (( result != 0 && SWITCHED == 1 )); then
    echo "Concat release failed: restoring only previous Concat service" >&2
    if [[ -n "$PREVIOUS" ]]; then
      ln -s "$PREVIOUS" "$ROOT/.rollback-$SHA"
      mv -Tf "$ROOT/.rollback-$SHA" "$ROOT/current"
      systemctl restart concat-web.service || true
    else
      systemctl stop concat-web.service || true
      rm -f "$ROOT/current"
    fi
  fi
  if [[ -n "${STAGE:-}" && -d "$STAGE" ]]; then rm -rf -- "$STAGE"; fi
  exit "$result"
}
trap cleanup_and_rollback EXIT
cp -- "$PACKAGE" "$STAGE/release.tgz"
chmod 0600 "$STAGE/release.tgz"
echo "$ARCHIVE_DIGEST  $STAGE/release.tgz" | sha256sum -c - >/dev/null ||
  fail "uploaded source archive checksum mismatch"

# Reject links, devices, traversal, duplicate paths and unexpectedly huge files.
python3 - "$STAGE/release.tgz" <<'PY'
import sys, tarfile
from pathlib import PurePosixPath
seen, size = set(), 0
with tarfile.open(sys.argv[1], mode='r:gz') as archive:
    for entry in archive:
        path = PurePosixPath(entry.name)
        if (path.is_absolute() or '..' in path.parts or not path.parts or
            entry.name.startswith('./') or not (entry.isfile() or entry.isdir()) or
            entry.name in seen or entry.size > 1073741824):
            sys.exit('Unsafe release archive entry')
        seen.add(entry.name)
        size += entry.size
        if size > 2147483648:
            sys.exit('Release archive exceeds size limit')
    if not {'run.py', 'pyproject.toml'}.issubset(seen):
        sys.exit('Missing application entrypoint')
PY
mkdir -m 0755 "$RELEASE"
tar --no-same-owner --no-same-permissions -xzf "$STAGE/release.tgz" -C "$RELEASE"
[[ -f "$RELEASE/run.py" && -d "$RELEASE/app" && -d "$RELEASE/reused" ]] ||
  fail "missing required Concat source"

# Package build is unprivileged. Make final release read-only for service user.
chown -R concatweb:concatweb "$RELEASE"
runuser -u concatweb -- python3 -m venv "$RELEASE/.venv"
runuser -u concatweb -- "$RELEASE/.venv/bin/python" -m pip install --no-input --disable-pip-version-check "$RELEASE"
chown -R root:root "$RELEASE"
chmod -R a+rX "$RELEASE"
chmod -R go-w "$RELEASE"
ln -s "$RELEASE" "$ROOT/.candidate-$SHA"
mv -Tf "$ROOT/.candidate-$SHA" "$ROOT/current"
SWITCHED=1
systemctl restart concat-web.service

HEALTHY=0
for i in {1..12}; do
  if curl --fail --silent --show-error --max-time 3 http://127.0.0.1:18873/healthz |
       grep -Fq '"service":"concat-web"'; then
    HEALTHY=1
    break
  fi
  sleep 2
done
[[ "$HEALTHY" == 1 ]] || fail "Concat-only loopback health failed"
for path in /api/v1/session /dashboard; do
  status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 6 "http://127.0.0.1:18873$path" || true)"
  [[ "$status" == 401 ]] || fail "anonymous private endpoint was not denied: $path"
done
rm -f -- "$PACKAGE"
echo "PASS: Concat SHA $SHA staged privately; public Nginx and VPS1 services unchanged"
