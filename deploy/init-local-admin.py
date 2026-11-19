#!/usr/bin/env python3
"""One-time, manual VPS terminal provisioning; never pass passwords in argv.

Creates only /etc/concat-web/concat-web.env. Existing private configuration
is never overwritten. Do not run as part of GitHub Actions or CI.
"""
from __future__ import annotations
import getpass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import sys

ROOT = Path("/etc/concat-web")
ENV = ROOT / "concat-web.env"


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run the private bootstrap with sudo on the VPS")
    if not ROOT.is_dir() or ROOT.is_symlink() or ROOT.stat().st_uid != 0:
        raise SystemExit("Concat private config directory is not root-owned")
    if ENV.exists() or ENV.is_symlink():
        raise SystemExit("Existing Concat private config will not be overwritten")
    username = input("Concat admin username [admin]: ").strip() or "admin"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", username) or username == "__default__":
        raise SystemExit("Invalid local admin username")
    password = getpass.getpass("Choose a new Concat admin password (16+ chars): ")
    confirmation = getpass.getpass("Confirm password: ")
    if (not hmac.compare_digest(password, confirmation) or
        not 16 <= len(password) <= 1024 or
        any(ord(char) < 32 for char in password)):
        raise SystemExit("Password mismatch or insufficient strength")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=16384, r=8, p=1, dklen=32)
    encoded = "scrypt$16384$8$1$" + salt.hex() + "$" + digest.hex()
    session_secret = secrets.token_urlsafe(48)
    uploader_secret = secrets.token_urlsafe(48)
    lines = [
        "CONCAT_AUTH_MODE=local-admin",
        "CONCAT_ADMIN_USERNAME=" + username,
        "CONCAT_ADMIN_PASSWORD_HASH=" + encoded,
        "CONCAT_ADMIN_SESSION_SECRET=" + session_secret,
        "CONCAT_WEB_SECURITY_DB=/var/lib/concat-web/security.sqlite3",
        "CONCAT_WEB_DATA_ROOT=/var/lib/concat-web",
        "CONCAT_WEB_PUBLIC_ORIGIN=https://concat.scenebuilder.online",
        "CONCAT_PUBLIC_ORIGIN=https://concat.scenebuilder.online",
        "CONCAT_UPLOADER_ENABLED=1",
        "LUM_SESSION_SECRET=" + uploader_secret,
        "LUM_DATA_DIR=/var/lib/concat-web/uploader",
        "CONCAT_CLOUD_RENDER_ROOT=/var/lib/concat-web",
        "CONCAT_WEB_HOST=127.0.0.1",
        "CONCAT_WEB_PORT=18873",
    ]
    content = ("\n".join(lines) + "\n").encode()
    # O_EXCL and O_NOFOLLOW prevent racing an existing private environment.
    fd = os.open(str(ENV), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        ENV.unlink(missing_ok=True)
        raise
    print("LOCAL_ADMIN_ENV=CREATED_MODE_0600")
    print("ADMIN_USERNAME=" + username)
    print("SECRETS=NOT_DISPLAYED")
    print("SERVICE=NOT_STARTED")


if __name__ == "__main__":
    main()
