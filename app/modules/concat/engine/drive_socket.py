"""Canonical Drive intake client. Credentials remain with the Scene service."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import socket

DEFAULT_SOCKET = "/run/concat-drive-bridge/intake.sock"
MAX_JSON = 16 * 1024 * 1024
MAX_ZIP = 4 * 1024 ** 3


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class DriveSocket:
    def __init__(self, owner, path=None):
        if not re.fullmatch(r"[a-f0-9]{64}", owner):
            raise ValueError("Invalid Drive Bridge owner")
        self.owner = owner
        self.path = path or os.getenv("CONCAT_DRIVE_BRIDGE_SOCKET", DEFAULT_SOCKET)

    def request(self, operation, obj, destination=None):
        connection = UnixHTTP(self.path, 7500 if destination else 5 if operation == "health" else 90)
        try:
            connection.request("POST", "/v1/" + operation,
                               json.dumps({"owner": self.owner, **obj}),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                try:
                    code = json.loads(response.read(4096)).get("error")
                except (ValueError, AttributeError):
                    code = "unavailable"
                message = {
                    "source_denied": "Google Drive source item permission denied",
                    "source_missing": "Google Drive source item missing or inaccessible",
                    "authentication": "Canonical Drive authentication expired or unauthorized",
                    "configuration": "Canonical Drive Bridge configuration unavailable",
                }.get(code, "Canonical Drive Bridge operation unavailable; intake stopped")
                raise RuntimeError(message)
            if destination is None:
                body = response.read(MAX_JSON + 1)
                if len(body) > MAX_JSON:
                    raise ValueError()
                return json.loads(body)
            size = int(response.getheader("Content-Length", "0"))
            sha = response.getheader("X-Content-SHA256", "")
            name = json.loads(response.getheader("X-Zip-Name", "null"))
            if (not 0 < size <= MAX_ZIP or not re.fullmatch(r"[a-f0-9]{64}", sha)
                    or not isinstance(name, str) or not name.lower().endswith(".zip")
                    or any(c in name for c in "/\\\r\n")):
                raise ValueError()
            digest, received = hashlib.sha256(), 0
            # Exclusive creation refuses symlinks and pre-existing files.
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    while chunk := response.read(min(1024 * 1024, size - received + 1)):
                        received += len(chunk)
                        if received > size:
                            raise ValueError()
                        digest.update(chunk)
                        stream.write(chunk)
                    if received != size or not hmac.compare_digest(digest.hexdigest(), sha):
                        raise ValueError()
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                Path(destination).unlink(missing_ok=True)
                raise
            return name
        except (OSError, http.client.HTTPException, ValueError, TypeError):
            raise RuntimeError("Canonical Drive Bridge unavailable or invalid response; intake stopped") from None
        finally:
            connection.close()

    def discover(self, link):
        from .cloud_render_production import drive_link, JobError, DRIVE_ID
        drive_link(link)
        obj = self.request("scan", {"link": link})
        files = obj.get("files") if isinstance(obj, dict) else None
        if not isinstance(files, list):
            raise JobError("Invalid canonical Drive Bridge listing")
        for row in files:
            if (not isinstance(row, list) or len(row) != 7
                    or not all(isinstance(row[i], str) for i in (0, 1, 2, 4, 5, 6))
                    or not DRIVE_ID.fullmatch(row[0]) or not row[1].lower().endswith(".zip")
                    or type(row[3]) is not int or not 0 <= row[3] <= MAX_ZIP):
                raise JobError("Invalid canonical Drive Bridge listing")
        return files

    def download(self, row, destination):
        from .cloud_render_production import JobError
        import shutil
        if not 0 <= row["drive_size"] <= MAX_ZIP:
            raise JobError("ZIP exceeds the Concat worker's 4 GiB upload limit.")
        required = row["drive_size"] or MAX_ZIP
        if shutil.disk_usage(destination.parent).free - 5_000_000_000 < required:
            raise JobError("Insufficient VPS disk reserve for the ZIP.")
        fields = ("drive_id", "name", "resource_keys", "drive_size", "drive_md5", "folder_id", "drive_path")
        return self.request("download", {"row": {key: row[key] for key in fields}}, destination)

    def available(self):
        try:
            return self.request("health", {}) == {"ok": True, "protocol": 1}
        except RuntimeError:
            return False
