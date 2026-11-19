#!/usr/bin/env python3
"""Adopt ONLY the audited Concat HTTPS location in an existing Nginx site.

This deliberately does not replace either server block, ACME locations,
certificate settings, port-80 redirects or other VPS1 vhosts.
Run manually on the VPS only after staging a CI-verified integration SHA.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

DOMAIN = "concat.scenebuilder.online"
AUDITED_HASH = "d0371b9ec8296a2e57dafafc2b7dda8268d70d23c0b657f5f46ea35e2c1819e5"
AVAILABLE = Path("/etc/nginx/sites-available/concat-web.conf")
ENABLED = Path("/etc/nginx/sites-enabled/concat-web.conf")
META = Path("/etc/concat-web/nginx-existing-adoption.json")
BACKUP_DIR = Path("/var/backups/concat-web/nginx")
ROOT = Path("/srv/concat-web")
ORIGIN = "https://concat.scenebuilder.online"
LOOPBACK = "http://127.0.0.1:18873"
PROXY = Path(__file__).with_name("nginx-proxy-location.conf")


def fail(code: str) -> None:
    raise RuntimeError("CONCAT_NGINX_BLOCKED=" + code)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def execute(*command: str) -> str:
    completed = subprocess.run(command, check=True, text=True, capture_output=True, timeout=30)
    return completed.stdout


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    # Temporary file stays in same directory: atomic update, no symlink follow.
    fd, name = tempfile.mkstemp(prefix=".concat-replace-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _end(text: str, opening: int) -> int:
    depth = 0
    for offset in range(opening, len(text)):
        if text[offset] == "{":
            depth += 1
        if text[offset] == "}":
            depth -= 1
            if depth == 0:
                return offset + 1
    fail("UNBALANCED_SERVER_OR_LOCATION")


def _blocks(text: str, expression: str):
    pattern = re.compile(r"(?m)^[ \t]*" + expression + r"[ \t]*\{")
    found = []
    for match in pattern.finditer(text):
        start = match.start()
        end = _end(text, match.end() - 1)
        found.append((start, end, text[start:end]))
    return found


def transform_existing_site(original: str, proxy_location: str) -> str:
    """Patch precisely one HTTPS location /; preserve all sibling directives."""
    if re.search(r"(?m)^\s*(proxy_pass|include)\s", original):
        fail("UNEXPECTED_EXISTING_PROXY_OR_INCLUDE")
    servers = _blocks(original, r"server")
    if len(servers) != 2:
        fail("EXPECTED_TWO_EXISTING_CONCAT_SERVER_BLOCKS")
    https = []
    for start, end, block in servers:
        names = re.findall(r"(?m)^\s*server_name\s+([^;]+);", block)
        if names != [DOMAIN]:
            fail("OTHER_SERVER_NAME_OR_ALIAS_PRESENT")
        if re.search(r"(?m)^\s*listen\s+443\s+ssl\s*;", block):
            https.append((start, end, block))
    if len(https) != 1:
        fail("EXPECTED_ONE_HTTPS_CONCAT_BLOCK")
    start, _, block = https[0]
    locations = _blocks(block, r"location\s+/")
    if len(locations) != 1:
        fail("EXPECTED_ONE_HTTPS_ROOT_LOCATION")
    offset, end, old_location = locations[0]
    if old_location.count("{") != 1 or old_location.count("}") != 1:
        fail("NESTED_ROOT_LOCATION_REQUIRES_REVIEW")
    if not re.search(r"(?m)^\s*try_files\s", old_location):
        fail("EXPECTED_AUDITED_STATIC_PLACEHOLDER")
    if not re.search(r"(?m)^\s*proxy_pass\s+http://127\.0\.0\.1:18873;", proxy_location):
        fail("INVALID_CONCAT_PROXY_LOCATION")
    if len(_blocks(proxy_location, r"location\s+/")) != 1:
        fail("INVALID_PROXY_BLOCK_COUNT")
    indent = re.match(r"[ \t]*", old_location).group()
    replacement = "\n".join(indent + line if line else "" for line in proxy_location.rstrip().splitlines())
    absolute_start, absolute_end = start + offset, start + end
    return original[:absolute_start] + replacement + original[absolute_end:]


def assert_known_site() -> bytes:
    if not ENABLED.is_symlink() or ENABLED.resolve() != AVAILABLE:
        fail("UNKNOWN_ENABLED_SITE_LINK")
    if AVAILABLE.is_symlink() or not AVAILABLE.is_file():
        fail("AVAILABLE_SITE_NOT_REGULAR")
    info = AVAILABLE.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o644 or info.st_nlink != 1:
        fail("UNEXPECTED_SITE_OWNERSHIP_PERMISSIONS")
    return AVAILABLE.read_bytes()


def private_root() -> None:
    conf = META.parent
    if (not conf.is_dir() or conf.is_symlink() or conf.stat().st_uid != 0 or
        stat.S_IMODE(conf.stat().st_mode) & 0o077):
        fail("CONCAT_PRIVATE_CONFIG_ROOT_INVALID")
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (BACKUP_DIR.is_symlink() or BACKUP_DIR.stat().st_uid != 0 or
        stat.S_IMODE(BACKUP_DIR.stat().st_mode) != 0o700):
        fail("CONCAT_NGINX_BACKUP_PATH_UNSAFE")


def _code(path: str, public: bool) -> str:
    url = (ORIGIN if public else LOOPBACK) + path
    command = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8"]
    if public:
        command += ["--resolve", DOMAIN + ":443:127.0.0.1"]
    return execute(*(command + [url])).strip()


def _health(public: bool) -> bool:
    url = (ORIGIN if public else LOOPBACK) + "/healthz"
    command = ["curl", "-fsS", "--max-time", "8"]
    if public:
        command += ["--resolve", DOMAIN + ":443:127.0.0.1"]
    try:
        result = json.loads(execute(*(command + [url])))
        return isinstance(result, dict) and result.get("service") == "concat-web" and result.get("ok") is True
    except (subprocess.SubprocessError, ValueError):
        return False


def assert_stage(sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        fail("INVALID_INTEGRATION_SHA")
    current = ROOT / "current"
    if not current.is_symlink() or current.resolve() != ROOT / "releases" / sha:
        fail("CONCAT_STAGE_SHA_MISMATCH")
    if execute("systemctl", "is-active", "concat-web.service").strip() != "active":
        fail("CONCAT_APP_SERVICE_INACTIVE")
    if not _health(False) or _code("/dashboard", False) != "401":
        fail("CONCAT_LOOPBACK_APP_SECURITY_NOT_READY")
    if _code("/auth/login", False) != "200":
        fail("CONCAT_LOCAL_ADMIN_LOGIN_NOT_READY")


def assert_nginx() -> None:
    execute("nginx", "-t")


def reload_nginx() -> None:
    execute("systemctl", "reload", "nginx")


def verify_https(scene_before: str) -> None:
    if not _health(True) or _code("/dashboard", True) != "401" or _code("/auth/login", True) != "200":
        fail("CONCAT_PUBLIC_HTTPS_IDENTITY_OR_AUTH_FAILED")
    if _code("/", True) == "000":
        fail("CONCAT_PUBLIC_HTTPS_UNAVAILABLE")
    if _code("https://app.scenebuilder.online/".replace("https://app.scenebuilder.online", ""),
             False) == "NEVER":
        fail("UNREACHABLE")  # intentionally unreachable; no effect


def scene_status() -> str:
    return execute("curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8",
                   "https://app.scenebuilder.online/").strip()


def activate(sha: str) -> None:
    if META.exists() or META.is_symlink():
        fail("EXISTING_ADOPTION_RECORD_REQUIRES_ROLLBACK_OR_REVIEW")
    assert_nginx()
    assert_stage(sha)
    old = assert_known_site()
    if digest(old) != AUDITED_HASH:
        fail("AUDITED_SITE_FINGERPRINT_DRIFT")
    private_root()
    proxy = PROXY.read_text()
    next_file = transform_existing_site(old.decode("utf-8"), proxy).encode()
    previous_scene = scene_status()
    if previous_scene == "000" or not previous_scene:
        fail("SCENE_BASELINE_NOT_REACHABLE")

    backup = BACKUP_DIR / ("original-" + AUDITED_HASH + ".conf")
    if backup.exists() or backup.is_symlink():
        fail("BACKUP_ALREADY_EXISTS_REQUIRES_MANUAL_REVIEW")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(old)
        handle.flush()
        os.fsync(handle.fileno())
    meta = {"sha": sha, "old_sha256": AUDITED_HASH, "new_sha256": digest(next_file),
            "backup": str(backup), "state": "pending"}
    fd = os.open(META, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(meta, handle)
        handle.flush()
        os.fsync(handle.fileno())

    changed = False
    try:
        atomic_write(AVAILABLE, next_file, 0o644)
        changed = True
        assert_nginx()
        reload_nginx()
        if not _health(True) or _code("/dashboard", True) != "401" or _code("/auth/login", True) != "200":
            fail("CONCAT_HTTPS_HEALTH_OR_AUTH_FAILED")
        if scene_status() != previous_scene:
            fail("SCENE_HTTPS_STATUS_CHANGED")
        meta["state"] = "active"
        atomic_write(META, json.dumps(meta).encode(), 0o600)
    except BaseException:
        if changed:
            atomic_write(AVAILABLE, old, 0o644)
            try:
                assert_nginx()
                reload_nginx()
                META.unlink()
            except Exception:
                print("CONCAT_NGINX_RECOVERY=MANUAL_REVIEW_REQUIRED", file=sys.stderr)
        else:
            META.unlink()
        raise
    print("CONCAT_EXISTING_HTTPS_ROUTE=ADOPTED")
    print("CONCAT_PUBLIC_HEALTH=VERIFIED")
    print("SCENE_HTTPS_STATUS=UNCHANGED")
    print("BACKUP=" + str(backup))


def rollback(sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        fail("INVALID_RELEASE_SHA")
    if not META.is_file() or META.is_symlink():
        fail("NO_ADOPTION_RECORD")
    meta = json.loads(META.read_text())
    if meta.get("sha") != sha or meta.get("old_sha256") != AUDITED_HASH:
        fail("ADOPTION_RECORD_MISMATCH")
    backup = Path(meta.get("backup", ""))
    if backup != BACKUP_DIR / ("original-" + AUDITED_HASH + ".conf"):
        fail("UNKNOWN_BACKUP_PATH")
    if not backup.is_file() or backup.is_symlink():
        fail("BACKUP_MISSING_OR_LINKED")
    original = backup.read_bytes()
    if digest(original) != AUDITED_HASH:
        fail("BACKUP_CHECKSUM_FAILURE")
    current = assert_known_site()
    if digest(current) != meta.get("new_sha256"):
        fail("CURRENT_SITE_EDITED_OUT_OF_BAND")
    assert_nginx()
    atomic_write(AVAILABLE, original, 0o644)
    try:
        assert_nginx()
        reload_nginx()
    except BaseException:
        atomic_write(AVAILABLE, current, 0o644)
        assert_nginx()
        reload_nginx()
        raise
    META.unlink()
    print("CONCAT_NGINX_ROLLBACK=PASS")
    print("STATIC_SITE_RESTORED=YES")
    print("OTHER_VPS_SITES=UNCHANGED")


def main(argv: list[str]) -> int:
    if len(argv) == 1 and argv[0] == "audit":
        original = assert_known_site()
        print("SITE_SHA256=" + digest(original))
        print("EXPECTED_STATIC_PLACEHOLDER=" + str(digest(original) == AUDITED_HASH))
        print("MANAGED_ADOPTION=" + str(META.exists()))
        print("CHANGES=NONE")
        return 0
    if os.geteuid() != 0:
        fail("ROOT_REQUIRED")
    if len(argv) != 2:
        fail("USAGE_audit_OR_activate_SHA_OR_rollback_SHA")
    command, sha = argv
    if command == "activate":
        activate(sha)
    elif command == "rollback":
        rollback(sha)
    else:
        fail("UNKNOWN_OPERATION")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (RuntimeError, OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
