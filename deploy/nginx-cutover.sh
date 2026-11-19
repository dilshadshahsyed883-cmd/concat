#!/usr/bin/env bash
# Concat-only HTTPS routing. All mutations are EXPLICIT and manual.
#   audit
#   activate <verified-integration-SHA> <fullchain.pem> <privkey.pem>
#   rollback <same-verified-integration-SHA>
# Never modifies Scene/VPS1 config or reloads Concat/Scene services.
set -Eeuo pipefail
umask 077

MODE="${1:-audit}"
SHA="${2:-}"
CERT="${3:-}"
KEY="${4:-}"
HOST="concat.scenebuilder.online"
APP="http://127.0.0.1:18873"
AVAILABLE="/etc/nginx/sites-available/concat-web.conf"
ENABLED="/etc/nginx/sites-enabled/concat-web.conf"
MARKER="/etc/concat-web/nginx-site-managed"
CURRENT="/srv/concat-web/current"

fail() { printf 'CONCAT_NGINX_BLOCKED=%s\n' "$*" >&2; exit 1; }
status_code() {
  curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "$1" 2>/dev/null || true
}
nginx_valid() { nginx -t >/dev/null 2>&1; }
site_reachable() {
  curl -fsS --max-time 8 --resolve "$HOST:443:127.0.0.1" \
      "https://$HOST/healthz" | grep -Fq '"service":"concat-web"'
}
anonymous_denied() {
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 \
      --resolve "$HOST:443:127.0.0.1" "https://$HOST/dashboard" 2>/dev/null || true)"
  [[ "$code" == 401 ]]
}
private_admin_page() {
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 \
      --resolve "$HOST:443:127.0.0.1" "https://$HOST/auth/login" 2>/dev/null || true)"
  [[ "$code" == 200 ]]
}
readiness() {
  [[ "$SHA" =~ ^[a-f0-9]{40}$ ]] || fail "EXACT_SHA_REQUIRED"
  [[ -L "$CURRENT" ]] || fail "CONCAT_RELEASE_NOT_SELECTED"
  [[ "$(readlink -f "$CURRENT")" == "/srv/concat-web/releases/$SHA" ]] ||
      fail "CONCAT_RELEASE_SHA_MISMATCH"
  systemctl is-active --quiet concat-web.service || fail "CONCAT_SERVICE_NOT_ACTIVE"
  [[ "$(status_code "$APP/healthz")" == 200 ]] || fail "CONCAT_LOOPBACK_UNHEALTHY"
  [[ "$(status_code "$APP/dashboard")" == 401 ]] || fail "ANONYMOUS_ACCESS_NOT_DENIED"
}
validate_certificate() {
  [[ "$CERT" == /* && "$KEY" == /* ]] || fail "ABSOLUTE_CERTIFICATE_PATHS_REQUIRED"
  [[ "$CERT" =~ ^/[A-Za-z0-9_./+-]+$ && "$KEY" =~ ^/[A-Za-z0-9_./+-]+$ ]] ||
      fail "UNSAFE_CERTIFICATE_PATH"
  [[ -f "$CERT" && -f "$KEY" ]] || fail "CERTIFICATE_OR_KEY_MISSING"
  openssl x509 -in "$CERT" -noout -checkhost "$HOST" 2>/dev/null |
      grep -Fq 'does match certificate' || fail "CERTIFICATE_HOSTNAME_MISMATCH"
  openssl x509 -in "$CERT" -noout -checkend 604800 >/dev/null 2>&1 ||
      fail "CERTIFICATE_EXPIRES_WITHIN_SEVEN_DAYS"
  local certificate_public key_public
  certificate_public="$(openssl x509 -in "$CERT" -pubkey -noout 2>/dev/null |
      openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)"
  key_public="$(openssl pkey -in "$KEY" -pubout -outform DER 2>/dev/null |
      sha256sum | cut -d' ' -f1)"
  [[ -n "$certificate_public" && "$certificate_public" == "$key_public" ]] ||
      fail "TLS_PRIVATE_KEY_MISMATCH"
}

case "$MODE" in
audit)
  echo "=== READ-ONLY CONCAT NGINX AUDIT ==="
  if nginx_valid; then echo "NGINX_SYNTAX=PASS"; else echo "NGINX_SYNTAX=FAIL"; fi
  if nginx -T 2>/dev/null |
     grep -Eq '^[[:space:]]*include[[:space:]]+/etc/nginx/sites-enabled/\*;'; then
    echo "SITES_ENABLED_INCLUDE=PRESENT"
  else
    echo "SITES_ENABLED_INCLUDE=NOT_CONFIRMED"
  fi
  if nginx -T 2>/dev/null |
     grep -Eq '^[[:space:]]*server_name[[:space:]]+concat\.scenebuilder\.online([[:space:];]|$)'; then
    echo "CONCAT_SERVER_NAME=CONFIGURED"
  else
    echo "CONCAT_SERVER_NAME=NOT_CONFIGURED"
  fi
  if [[ -e "$ENABLED" || -L "$ENABLED" ]]; then
    echo "CONCAT_SITE_LINK=EXISTS"
  else
    echo "CONCAT_SITE_LINK=ABSENT"
  fi
  if [[ -f "$MARKER" ]]; then echo "MANAGED_CUTOVER_MARKER=PRESENT"
  else echo "MANAGED_CUTOVER_MARKER=ABSENT"; fi
  if getent ahostsv4 "$HOST" >/dev/null 2>&1; then echo "DNS_IPV4=RESOLVES"
  else echo "DNS_IPV4=NOT_RESOLVED"; fi
  echo "CONCAT_LOOPBACK_HEALTH_HTTP=$(status_code "$APP/healthz")"
  echo "CONCAT_LOOPBACK_PRIVATE_HTTP=$(status_code "$APP/dashboard")"
  echo "EXISTING_SCENE_HTTPS_HTTP=$(status_code 'https://app.scenebuilder.online/')"
  echo "CHANGES=NONE"
  ;;
activate)
  [[ "$EUID" -eq 0 ]] || fail "ROOT_REQUIRED"
  command -v openssl >/dev/null || fail "OPENSSL_MISSING"
  nginx_valid || fail "CURRENT_NGINX_INVALID"
  [[ -d /etc/nginx/sites-available && -d /etc/nginx/sites-enabled ]] ||
      fail "NGINX_SITE_DIRECTORIES_NOT_FOUND"
  [[ -d /etc/concat-web && ! -L /etc/concat-web ]] || fail "CONCAT_CONFIG_ROOT_MISSING"
  [[ ! -e "$AVAILABLE" && ! -L "$AVAILABLE" &&
     ! -e "$ENABLED" && ! -L "$ENABLED" && ! -e "$MARKER" ]] ||
      fail "CONCAT_ROUTE_ALREADY_EXISTS_REVIEW_MANUALLY"
  nginx -T 2>/dev/null |
    grep -Eq '^[[:space:]]*include[[:space:]]+/etc/nginx/sites-enabled/\*;' ||
      fail "NGINX_DOES_NOT_INCLUDE_SITES_ENABLED"
  if nginx -T 2>/dev/null |
     grep -Eq '^[[:space:]]*server_name[[:space:]]+concat\.scenebuilder\.online([[:space:];]|$)'; then
      fail "EXISTING_CONCAT_SERVER_NAME_CONFLICT"
  fi
  readiness
  validate_certificate
  BEFORE="$(status_code 'https://app.scenebuilder.online/')"
  [[ "$BEFORE" =~ ^[1-5][0-9][0-9]$ && "$BEFORE" != 000 ]] ||
      fail "EXISTING_SITE_BASELINE_UNAVAILABLE"
  TEMPLATE="$CURRENT/deploy/nginx-site.example.conf"
  [[ -f "$TEMPLATE" && ! -L "$TEMPLATE" ]] || fail "RELEASE_NGINX_TEMPLATE_MISSING"
  TMP="$(mktemp)"
  FILE_CREATED=0
  LINK_CREATED=0
  cleanup() {
    local exit_code=$?
    trap - EXIT
    rm -f -- "${TMP:-}"
    if (( exit_code != 0 && (FILE_CREATED == 1 || LINK_CREATED == 1) )); then
      echo "CONCAT_NGINX_ROLLBACK=STARTED" >&2
      if (( LINK_CREATED == 1 )); then rm -f -- "$ENABLED"; fi
      if (( FILE_CREATED == 1 )); then rm -f -- "$AVAILABLE"; fi
      rm -f -- "$MARKER"
      if nginx_valid; then
        systemctl reload nginx || echo "CONCAT_NGINX_RECOVERY_REQUIRES_OPERATOR=YES" >&2
      else
        echo "CONCAT_NGINX_RECOVERY_REQUIRES_OPERATOR=YES" >&2
      fi
    fi
    exit "$exit_code"
  }
  trap cleanup EXIT
  python3 - "$TEMPLATE" "$TMP" "$CERT" "$KEY" <<'PY'
from pathlib import Path
import sys
source, target, cert, key = map(Path, sys.argv[1:])
text = source.read_text()
assert text.count('__CONCAT_TLS_FULLCHAIN__') == 1
assert text.count('__CONCAT_TLS_PRIVKEY__') == 1
for entry in (cert, key):
    if any(ch in str(entry) for ch in ' ;{}$"\\\t\r\n'):
        raise SystemExit('Unsafe TLS path')
text = text.replace('__CONCAT_TLS_FULLCHAIN__', str(cert))
text = text.replace('__CONCAT_TLS_PRIVKEY__', str(key))
target.write_text(text)
PY
  # The site must be NEW, so no older application configuration is overwritten.
  ( set -C; umask 022; cat "$TMP" > "$AVAILABLE" )
  FILE_CREATED=1
  chown root:root "$AVAILABLE"
  chmod 0644 "$AVAILABLE"
  ln -s "$AVAILABLE" "$ENABLED"
  LINK_CREATED=1
  nginx_valid || fail "NGINX_CONFIG_TEST_FAILED"
  systemctl reload nginx || fail "NGINX_RELOAD_FAILED"
  site_reachable || fail "CONCAT_HTTPS_HEALTH_FAILED"
  anonymous_denied || fail "CONCAT_ANONYMOUS_DENIAL_FAILED"
  private_admin_page || fail "CONCAT_HTTPS_LOGIN_UNAVAILABLE"
  AFTER="$(status_code 'https://app.scenebuilder.online/')"
  [[ "$BEFORE" == "$AFTER" ]] || fail "EXISTING_SITE_HTTP_CHANGED"
  DIGEST="$(sha256sum "$AVAILABLE" | cut -d' ' -f1)"
  ( set -C; printf '%s %s\n' "$SHA" "$DIGEST" > "$MARKER" )
  chmod 0600 "$MARKER"
  echo "CONCAT_NGINX_HTTPS=ACTIVE"
  echo "CONCAT_HTTPS_HEALTH=PASS"
  echo "CONCAT_ANONYMOUS_DENIAL=PASS"
  echo "EXISTING_SCENE_SITE=UNCHANGED_HTTP_STATUS"
  echo "OTHER_VPS_SERVICES=NOT_TOUCHED"
  ;;
rollback)
  [[ "$EUID" -eq 0 ]] || fail "ROOT_REQUIRED"
  [[ "$SHA" =~ ^[a-f0-9]{40}$ ]] || fail "EXACT_SHA_REQUIRED"
  [[ -f "$MARKER" && ! -L "$MARKER" ]] || fail "NO_MANAGED_ROUTE_MARKER"
  [[ -f "$AVAILABLE" && ! -L "$AVAILABLE" && -L "$ENABLED" ]] ||
      fail "ROUTE_NOT_MANAGED_BY_THIS_TOOL"
  [[ "$(readlink "$ENABLED")" == "$AVAILABLE" ]] ||
      fail "UNEXPECTED_NGINX_LINK_TARGET"
  read -r ACTIVE_SHA DIGEST < "$MARKER"
  [[ "$ACTIVE_SHA" == "$SHA" ]] || fail "MANAGED_SHA_MISMATCH"
  [[ "$(sha256sum "$AVAILABLE" | cut -d' ' -f1)" == "$DIGEST" ]] ||
      fail "NGINX_CONFIG_DRIFT_REVIEW_REQUIRED"
  nginx_valid || fail "CURRENT_NGINX_INVALID"
  rm -- "$ENABLED"
  if ! nginx_valid || ! systemctl reload nginx; then
    ln -s "$AVAILABLE" "$ENABLED"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
    fail "ROLLBACK_FAILED_ROUTE_RESTORED_FOR_REVIEW"
  fi
  rm -- "$AVAILABLE" "$MARKER"
  echo "CONCAT_NGINX_ROUTE=REMOVED"
  echo "OTHER_NGINX_SITES=NOT_MODIFIED"
  ;;
*) fail "USE_audit_activate_or_rollback" ;;
esac
