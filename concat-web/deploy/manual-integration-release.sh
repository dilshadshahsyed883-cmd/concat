#!/usr/bin/env bash
# Manual only: stage an exact Web CI-verified main-line SHA on Concat's loopback service.
# The filename is a legacy compatibility alias, but only canonical main is allowed.
# Never changes Scene, VPS1 deploy helpers, Nginx, Drive Bridge, OAuth or Salad.
set -Eeuo pipefail
set +x
umask 077

fail() { printf 'BLOCKED: %s\n' "$*" >&2; exit 1; }
# The approved SHA must be a real main-line commit; compare only Web inputs.
# The operator must verify successful Web essential/security CI for that SHA.
verify_web_release() {
  local repo="$1" approved="$2" main_tip="$3" approved_tree main_tree
  [[ "$(git -C "$repo" rev-parse --verify "$approved^{commit}")" == "$approved" ]] ||
    fail 'Invalid approved commit'
  git -C "$repo" rev-list --first-parent "$main_tip" | grep -Fx "$approved" >/dev/null ||
    fail 'Approved SHA is not on canonical main first-parent history'
  approved_tree="$(git -C "$repo" rev-parse --verify "$approved:concat-web")" ||
    fail 'Approved Web tree missing'
  main_tree="$(git -C "$repo" rev-parse --verify "$main_tip:concat-web")" ||
    fail 'Current Web tree missing'
  [[ "$(git -C "$repo" cat-file -t "$approved_tree")" == tree ]] ||
    fail 'Approved Web path is not a tree'
  [[ "$approved_tree" == "$main_tree" ]] ||
    fail 'Web source changed; approve a fresh Web CI-verified main SHA'
  git -C "$repo" diff --quiet --no-ext-diff "$approved" "$main_tip" -- \
    ':(glob).github/workflows/concat-web-*.yml' ||
    fail 'Web workflow changed; approve a fresh Web CI-verified main SHA'
  printf 'CONCAT_WEB_SOURCE_VERIFIED=PASS\nWEB_TREE=%s\nAPPROVED_SHA=%s\nMAIN_SHA=%s\n' \
    "$approved_tree" "$approved" "$main_tip"
}

# Sourcing exposes only the offline verification function for regression tests.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then return; fi

[[ "$#" -eq 2 ]] || fail 'Usage: sudo bash manual-integration-release.sh <40-hex-SHA> stage|preflight'
SHA="$1"
MODE="$2"
[[ "$SHA" =~ ^[a-f0-9]{40}$ ]] || fail 'Invalid exact main SHA'
[[ "$MODE" == stage || "$MODE" == preflight ]] || fail 'Invalid mode'
[[ "$EUID" == 0 ]] || fail 'Run only as root in VPS terminal'
REPO='https://github.com/dilshadshahsyed883-cmd/concat.git'
BRANCH='main'
for tool in git python3 tar sha256sum systemctl curl nginx flock install runuser; do
  command -v "$tool" >/dev/null || fail "Missing prerequisite $tool"
done
[[ -d /etc/concat-web && ! -L /etc/concat-web ]] || fail 'Private Concat config folder missing'
[[ -d /srv/concat-web/releases && ! -L /srv/concat-web/releases ]] || fail 'Concat releases missing'
[[ -d /var/lib/concat-web && ! -L /var/lib/concat-web ]] || fail 'Private Concat data missing'
[[ -d /var/lib/concat-web/uploader && ! -L /var/lib/concat-web/uploader ]] || fail 'Private uploader data missing'
id concatweb >/dev/null || fail 'Concat user missing'
id vps1deploy >/dev/null || fail 'VPS deploy identity missing'
systemctl cat concat-web.service >/dev/null 2>&1 || fail 'Separate Concat unit missing'
[[ -x /usr/local/sbin/concat-web-deploy && ! -L /usr/local/sbin/concat-web-deploy ]] ||
  fail 'Installed, non-symlinked Concat helper required'
[[ "$(stat -c %U /usr/local/sbin/concat-web-deploy)" == root ]] ||
  fail 'Unexpected installed helper owner'
nginx -t >/dev/null 2>&1 || fail 'Existing Nginx configuration invalid'

exec 8>/run/lock/concat-main-terminal-release.lock
flock -n 8 || fail 'Another Concat main release running'
export GIT_TERMINAL_PROMPT=0

WORK="$(mktemp -d /root/concat-main-release.XXXXXXXX)"
BACKUP_BASE='/var/backups/concat-web'
[[ ! -L "$BACKUP_BASE" ]] || fail 'Unsafe backup folder'
install -d -o root -g root -m 0700 "$BACKUP_BASE"
BACKUP="$(mktemp -d "$BACKUP_BASE/release.XXXXXXXX")"
HELPER_CHANGED=0
DEPLOY_COMPLETED=0
SUCCESS=0
cleanup() {
  code=$?
  trap - EXIT
  if (( code != 0 && DEPLOY_COMPLETED == 1 && SUCCESS == 0 )); then
    if [[ -f "$BACKUP/previous-release-path" ]]; then
      previous="$(cat "$BACKUP/previous-release-path")"
      if [[ "$previous" == /srv/concat-web/releases/* && -d "$previous" ]]; then
        ln -s "$previous" /srv/concat-web/.manual-rollback-"$SHA"
        mv -Tf /srv/concat-web/.manual-rollback-"$SHA" /srv/concat-web/current
        systemctl restart concat-web.service || true
      fi
    else
      systemctl stop concat-web.service || true
      rm -f /srv/concat-web/current
    fi
    printf 'Concat-only staged release restored after post-deploy smoke failure.\n' >&2
  fi
  if (( code != 0 && HELPER_CHANGED == 1 && SUCCESS == 0 )); then
    install -o root -g root -m 0750 "$BACKUP/previous-helper" /usr/local/sbin/concat-web-deploy || true
    printf 'Previous Concat helper restored after failure.\n' >&2
  fi
  rm -rf -- "$WORK"
  exit "$code"
}
trap cleanup EXIT

git clone --quiet --no-checkout --single-branch --branch "$BRANCH" "$REPO" "$WORK/source"
REMOTE="$(git -C "$WORK/source" rev-parse --verify "refs/remotes/origin/$BRANCH")"
verify_web_release "$WORK/source" "$SHA" "$REMOTE"
git -C "$WORK/source" checkout --quiet --detach "$SHA"
[[ "$(git -C "$WORK/source" rev-parse HEAD)" == "$SHA" ]] ||
  fail 'Cloned Git source differs from approved main SHA'
[[ -f "$WORK/source/concat-web/deploy/guarded-release.sh" ]] || fail 'Missing guarded helper'
[[ -f "$WORK/source/concat-web/deploy/init-local-admin.py" ]] || fail 'Missing private admin setup'
bash -n "$WORK/source/concat-web/deploy/guarded-release.sh" || fail 'Helper shell syntax invalid'

# Keep rollback copies private. Never display env values or contents.
cp -p /usr/local/sbin/concat-web-deploy "$BACKUP/previous-helper"
if [[ -f /etc/concat-web/concat-web.env && ! -L /etc/concat-web/concat-web.env ]]; then
  cp -p /etc/concat-web/concat-web.env "$BACKUP/previous-env"
elif [[ -e /etc/concat-web/concat-web.env || -L /etc/concat-web/concat-web.env ]]; then
  fail 'Unsafe Concat environment path'
fi
if [[ -L /srv/concat-web/current ]]; then
  readlink -f /srv/concat-web/current > "$BACKUP/previous-release-path"
elif [[ -e /srv/concat-web/current ]]; then
  fail 'Unexpected non-symlink current Concat release'
fi

# SQLite's online backup API avoids inconsistent raw WAL copies. No media is deleted.
python3 - /var/lib/concat-web "$BACKUP" <<'PY'
from pathlib import Path
import sqlite3, sys
from urllib.parse import quote
root, backup = Path(sys.argv[1]), Path(sys.argv[2]) / 'sqlite'
for source in root.rglob('*.sqlite3'):
    if source.is_symlink() or not source.is_file():
        raise SystemExit('Unsafe local SQLite state; release blocked')
    relative = source.relative_to(root)
    target = backup / relative
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    uri = 'file:' + quote(str(source), safe='/') + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=10) as old, sqlite3.connect(target) as saved:
        old.backup(saved)
    target.chmod(0o600)
print('CONCAT_SQLITE_BACKUP=PASS')
PY

# Replace only Concat helper. Never modify the VPS1 production deploy helper.
install -o root -g root -m 0750 "$WORK/source/concat-web/deploy/guarded-release.sh" \
    /usr/local/sbin/concat-web-deploy
HELPER_CHANGED=1
if [[ ! -e /etc/concat-web/concat-web.env ]]; then
  [[ -r /dev/tty ]] || fail 'Terminal required for private admin password setup'
  # Host-only interactive getpass; no credentials in shell arguments or logs.
  python3 "$WORK/source/concat-web/deploy/init-local-admin.py" < /dev/tty
fi
/usr/local/sbin/concat-web-deploy preflight "$SHA"
printf 'CONCAT_MAIN_PREFLIGHT=PASS\n'
if [[ "$MODE" == preflight ]]; then
  SUCCESS=1
  printf 'DEPLOYMENT=NOT_PERFORMED\nBACKUP_PATH=%s\n' "$BACKUP"
  exit 0
fi

ARCHIVE="/tmp/concat-web-$SHA.tgz"
[[ ! -e "$ARCHIVE" && ! -L "$ARCHIVE" ]] || fail 'Exact release archive path already exists'
tar --sort=name --mtime='UTC 2020-01-01' --owner=0 --group=0 \
    --numeric-owner -czf "$ARCHIVE" -C "$WORK/source/concat-web" \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    app reused deploy run.py pyproject.toml
chmod 0600 "$ARCHIVE"
chown vps1deploy "$ARCHIVE"
DIGEST="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
/usr/local/sbin/concat-web-deploy deploy "$SHA" "$ARCHIVE" "$DIGEST"
DEPLOY_COMPLETED=1
curl -fsS --max-time 6 http://127.0.0.1:18873/healthz >/dev/null ||
  fail 'Concat loopback health unavailable after staging'
[[ "$(curl -sS --max-time 6 -o /dev/null -w '%{http_code}' \
     http://127.0.0.1:18873/dashboard)" == 401 ]] ||
  fail 'Anonymous dashboard is not denied'
[[ "$(curl -sS --max-time 6 -o /dev/null -w '%{http_code}' \
     http://127.0.0.1:18873/auth/login)" == 200 ]] ||
  fail 'Local admin login page unavailable'
SUCCESS=1
printf 'CONCAT_LOOPBACK_STAGE=PASS\nPUBLIC_NGINX_CUTOVER=NOT_PERFORMED\n'
printf 'STAGED_SHA=%s\nBACKUP_PATH=%s\n' "$SHA" "$BACKUP"
