#!/usr/bin/env bash
# Root-owned, Concat-only deploy helper installed at /usr/local/sbin/concat-web-deploy.
# Manual terminal only: never modifies Scene, Nginx, Drive Bridge, Salad or VPS1 units.
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
# The parent may be writable while a previously root-provisioned uploader
# directory is not. This produces a live /concat-uploader/* 503 even when
# /healthz and the installation package are healthy.
UPLOADER_ROOT='/var/lib/concat-web/uploader'
[[ -d "$UPLOADER_ROOT" && ! -L "$UPLOADER_ROOT" ]] ||
  fail "Dedicated Concat Uploader state missing or unsafe"
for path in "$UPLOADER_ROOT" "$UPLOADER_ROOT/core" "$UPLOADER_ROOT/secure" "$UPLOADER_ROOT/staging"; do
  [[ ! -L "$path" ]] || fail "Concat Uploader runtime symlink blocked"
  if [[ -e "$path" ]]; then
    [[ -d "$path" ]] || fail "Concat Uploader runtime path must be a directory"
    runuser -u concatweb -- test -r "$path" &&
      runuser -u concatweb -- test -w "$path" &&
      runuser -u concatweb -- test -x "$path" ||
      fail "Concat Uploader storage permissions invalid for concatweb"
  fi
done
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

# Only the Concat local administrator is an identity source. Never read
# the live Scene database or trust old SSO tickets/headers.
python3 - <<'PY'
from pathlib import Path
import os
import re
import stat
import sys

env = Path('/etc/concat-web/concat-web.env')
values = {}
for line in env.read_text().splitlines():
    if not line.strip() or line.lstrip().startswith('#'):
        continue
    if '=' not in line:
        sys.exit('Invalid local Concat environment line')
    key, value = line.split('=', 1)
    key = key.strip()
    if key in values:
        sys.exit('Duplicate local Concat environment setting')
    values[key] = value.strip().strip('"').strip("'")

required = (
    'CONCAT_AUTH_MODE', 'CONCAT_ADMIN_USERNAME',
    'CONCAT_ADMIN_PASSWORD_HASH', 'CONCAT_ADMIN_SESSION_SECRET',
    'CONCAT_WEB_SECURITY_DB', 'CONCAT_WEB_DATA_ROOT',
    'CONCAT_WEB_PUBLIC_ORIGIN', 'CONCAT_PUBLIC_ORIGIN',
    'CONCAT_UPLOADER_ENABLED', 'LUM_SESSION_SECRET',
    'LUM_DATA_DIR', 'CONCAT_CLOUD_RENDER_ROOT',
    'CONCAT_WEB_HOST', 'CONCAT_WEB_PORT',
)
if any(not values.get(k) for k in required):
    sys.exit('Missing private local admin/uploader configuration')
exact = {
    'CONCAT_AUTH_MODE': 'local-admin',
    'CONCAT_WEB_PUBLIC_ORIGIN': 'https://concat.scenebuilder.online',
    'CONCAT_PUBLIC_ORIGIN': 'https://concat.scenebuilder.online',
    'CONCAT_WEB_HOST': '127.0.0.1',
    'CONCAT_WEB_PORT': '18873',
    'CONCAT_WEB_DATA_ROOT': '/var/lib/concat-web',
    'CONCAT_WEB_SECURITY_DB': '/var/lib/concat-web/security.sqlite3',
    'CONCAT_UPLOADER_ENABLED': '1',
    'LUM_DATA_DIR': '/var/lib/concat-web/uploader',
    'CONCAT_CLOUD_RENDER_ROOT': '/var/lib/concat-web',
}
if any(values.get(k) != v for k, v in exact.items()):
    sys.exit('Unexpected Concat isolation or HTTPS setting')
if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', values['CONCAT_ADMIN_USERNAME']):
    sys.exit('Unsafe local admin username')
if values['CONCAT_ADMIN_USERNAME'] == '__default__':
    sys.exit('Legacy owner forbidden')
if len(values['CONCAT_ADMIN_SESSION_SECRET'].encode()) < 32 or len(values['LUM_SESSION_SECRET'].encode()) < 32:
    sys.exit('Private session secret too short')
if values['CONCAT_ADMIN_SESSION_SECRET'] == values['LUM_SESSION_SECRET']:
    sys.exit('Admin and uploader signing secrets must be distinct')
try:
    scheme, n, r, p, salt, digest = values['CONCAT_ADMIN_PASSWORD_HASH'].split('$')
    valid_hash = (scheme == 'scrypt' and (int(n), int(r), int(p)) == (16384,8,1)
                  and len(bytes.fromhex(salt)) == 16
                  and len(bytes.fromhex(digest)) == 32)
except (ValueError, TypeError):
    valid_hash = False
if not valid_hash:
    sys.exit('Unsupported or malformed local password hash')
for path_str in ('/var/lib/concat-web','/var/lib/concat-web/uploader'):
    folder = Path(path_str)
    if not folder.is_dir() or folder.is_symlink() or stat.S_IMODE(folder.stat().st_mode) != 0o700:
        sys.exit('Private Concat data directory invalid')
db = Path('/var/lib/concat-web/security.sqlite3')
if db.exists():
    info = db.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        sys.exit('Local Concat auth DB is not private')
print('Concat private local-admin and uploader readiness: PASS')
PY
if [[ "$MODE" == preflight ]]; then
  echo "PASS: isolated Concat service and private local-admin configuration; NO deployment"
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
