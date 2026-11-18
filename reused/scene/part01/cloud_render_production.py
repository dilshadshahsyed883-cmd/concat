"""Persistent, sequential Drive-to-Concat dispatch using the existing worker API."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from fastapi import HTTPException

LOG = logging.getLogger(__name__)
MAX_ZIP = 4 * 1024 ** 3  # Concat GPU resumable upload limit
MAX_MANIFEST = 64 * 1024 ** 2
LEGACY_MANIFEST_LIMIT = 2 * 1024 ** 2
MODE = "cuda-hybrid-v1"
CHUNK = 8 * 1024 ** 2
DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
SHA256 = re.compile(r"[0-9a-f]{64}")
TERMINAL = ("Completed", "Failed", "Cancelled")


class JobError(RuntimeError):
    pass


class WorkerWait(JobError):
    pass


class Checkpoint(Exception):
    pass


def drive_link(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise JobError("Enter a Google Drive folder or ZIP file link.")
    try:
        url = urlsplit(value.strip())
        query = parse_qs(url.query, max_num_fields=24)
    except ValueError:
        raise JobError("Invalid Google Drive link.") from None
    if url.scheme != "https" or url.netloc.lower() != "drive.google.com":
        raise JobError("Use an HTTPS link from drive.google.com.")
    folder = re.fullmatch(r"/drive/(?:u/\d+/)?folders/([\w-]+)/?", url.path)
    file = re.fullmatch(r"/file/(?:u/\d+/)?d/([\w-]+)(?:/(?:view|preview|edit))?/?", url.path)
    ids = query.get("id", [])
    item = (folder or file).group(1) if folder or file else (
        ids[0] if url.path in ("/open", "/uc") and len(ids) == 1 else "")
    keys = query.get("resourcekey", [""])
    if not DRIVE_ID.fullmatch(item) or len(keys) != 1 or (keys[0] and not DRIVE_ID.fullmatch(keys[0])):
        raise JobError("Invalid Drive file ID or resource key.")
    return item, keys[0], bool(folder)


class DriveIntake:
    def __init__(self, execute):
        self.execute = execute

    @staticmethod
    def remote():
        remote = os.getenv("CLOUD_RENDER_DRIVE_REMOTE", "gdrive")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", remote):
            raise JobError("Invalid canonical Drive Bridge remote.")
        return remote + ":"

    @staticmethod
    def resource_header(keys):
        return ["--header", "X-Goog-Drive-Resource-Keys: " + keys] if keys else []

    def discover(self, link):
        item, key, folder = drive_link(link)
        keys = f"{item}/{key}" if key else ""
        if not folder:
            return [(item, item + ".zip", keys, 0, "", "", "")]
        cmd = ["rclone", "lsjson", self.remote(), "--drive-root-folder-id", item,
               "--recursive", "--files-only", "--drive-skip-shortcuts", "--hash"]
        if key:
            cmd.extend(["--drive-resource-key", key])
        try:
            entries = json.loads(self.execute(cmd, 60))
        except ValueError:
            raise JobError("Drive Bridge returned an invalid folder listing.") from None
        if not isinstance(entries, list):
            raise JobError("Drive Bridge returned an invalid folder listing.")
        files = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            file_id, name = entry.get("ID"), entry.get("Name")
            if (not isinstance(file_id, str) or not DRIVE_ID.fullmatch(file_id)
                    or not isinstance(name, str) or not name.lower().endswith(".zip")):
                continue
            hashes = entry.get("Hashes") or {}
            md5 = (hashes.get("MD5") or hashes.get("md5") or "").lower()
            file_key = entry.get("ResourceKey") or ""
            header = ",".join(filter(None, (keys, f"{file_id}/{file_key}" if DRIVE_ID.fullmatch(file_key) else "")))
            size = entry.get("Size")
            path = entry.get("Path")
            safe_path = isinstance(path, str) and all(part not in ("", ".", "..") for part in path.split("/"))
            files.append((file_id, name[:240], header, size if type(size) is int else 0, md5,
                          item, path if safe_path else ""))
        return files

    def download(self, row, destination):
        if row["drive_size"] > MAX_ZIP:
            raise JobError("ZIP exceeds the Concat worker's 4 GiB upload limit.")
        limit = min(MAX_ZIP, shutil.disk_usage(destination.parent).free - 5_000_000_000)
        if limit < max(1, row["drive_size"]):
            raise JobError("Insufficient VPS disk reserve for the ZIP.")
        # The destination is local and private. copyid resolves the exact Drive
        # file even when two folder children have the same display name.
        flags = ["--max-transfer", f"{limit}B", "--cutoff-mode", "CAUTIOUS",
                 "--multi-thread-streams", "0", "--low-level-retries", "3"]
        remote = self.remote()
        folder = row["folder_id"]
        target = destination
        if not folder:
            # A direct Drive link has no metadata in its URL. Let rclone use
            # the remote display name in a private one-file directory.
            target = destination.with_suffix(".download")
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(mode=0o700)
        cmd = ["rclone", "backend", "copyid", remote, row["drive_id"],
               str(target) + ("" if folder else "/")]
        try:
            self.execute(cmd + flags + self.resource_header(row["resource_keys"]), 3600)
        except RuntimeError as exc:
            # Listing a keyed parent lets rclone learn each child's resource
            # key. A fallback by name is allowed only when the scan provided a
            # content checksum, which the queue verifies before dispatch.
            category = getattr(exc, "category", "")
            if not folder or not row["drive_path"] or not row["drive_md5"] or not (
                    "permission" in category or category == "remote_file_absent"):
                raise
            root_key = next((pair.split("/", 1)[1] for pair in row["resource_keys"].split(",")
                             if pair.startswith(folder + "/")), "")
            root_flags = ["--drive-root-folder-id", folder, "--drive-skip-shortcuts"]
            if root_key:
                root_flags.extend(["--drive-resource-key", root_key])
            self.execute(["rclone", "copyto", remote + row["drive_path"], str(destination)]
                         + flags + root_flags, 3600)
        if folder:
            return row["name"]
        try:
            files = [entry for entry in target.iterdir() if not entry.is_symlink() and entry.is_file()]
            if len(files) != 1:
                raise JobError("Drive Bridge did not return a single ZIP file.")
            os.replace(files[0], destination)
            return files[0].name[:240]
        finally:
            shutil.rmtree(target)



def bundle_manifest(path):
    """Read the original JSON without extracting or changing the uploaded ZIP."""
    try:
        with path.open("rb") as stream:
            # ZipFile loads the full central directory. Bound it before allocation,
            # including for ZIP64 bundles produced by the Desktop packer.
            end = zipfile._EndRecData(stream)
        if not end or end[zipfile._ECD_ENTRIES_TOTAL] > 20_002 or end[zipfile._ECD_SIZE] > 32 * 1024 ** 2:
            raise JobError("Invalid ZIP or oversized ZIP directory.")
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            members = {entry.filename: entry for entry in entries}
            if len(entries) != len(members) or any(
                stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1 for entry in entries
            ):
                raise JobError("ZIP contains duplicate, linked, or encrypted entries.")
            if sum(entry.file_size for entry in entries) > 500 * 1024 ** 3:
                raise JobError("Concat ZIP exceeds the worker's extraction limit.")
            control_info = members["bundle.json"]
            if control_info.file_size > 16 * 1024 ** 2:
                raise JobError("Concat bundle metadata is too large.")
            control = json.loads(archive.read(control_info))
            project = control["project_file"]
            refs = control["references"]
            if control["version"] != 1 or project not in ("concat.json", "wolfcut.json"):
                raise JobError("Unsupported Concat ZIP format. Use Prepare ZIP Only in Concat.")
            if not isinstance(refs, list) or len(refs) > 20_000:
                raise JobError("Invalid Concat media references.")
            info = members["project/" + project]
            if not 0 < info.file_size <= MAX_MANIFEST:
                raise JobError("Concat project manifest exceeds the 64 MiB worker limit.")
            manifest = archive.read(info).removeprefix(b"\xef\xbb\xbf")
            media = json.loads(manifest)["media"]
            if not isinstance(media, list):
                raise JobError("Invalid Concat project media list.")
            expected = {"bundle.json", info.filename}
            verified = {}
            for ref in refs:
                member, index, digest, size = (ref[k] for k in ("member", "index", "sha256", "size"))
                if (not re.fullmatch(r"media/[a-f0-9]{32}(?:\.[a-z0-9]{1,12})?", member)
                        or type(index) is not int or not 0 <= index < len(media)
                        or not isinstance(media[index], dict) or not SHA256.fullmatch(digest)
                        or type(size) is not int or size != members[member].file_size
                        or verified.get(member, digest) != digest):
                    raise JobError("Concat ZIP has invalid media references.")
                verified[member] = digest
                expected.add(member)
            if expected != members.keys():
                raise JobError("Concat ZIP contains unexpected files.")
            return manifest
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError, OSError, RecursionError, NotImplementedError):
        raise JobError("Invalid Concat ZIP. Use a bundle made with Prepare ZIP Only.") from None


class Worker:
    def __init__(self, endpoint, token, salad_key, health, timeout):
        self.endpoint, self.health, self.timeout = endpoint, health, timeout
        self.headers = {"Authorization": "Bearer " + token}
        if salad_key:
            self.headers["Salad-Api-Key"] = salad_key

    def request(self, method, path, *, missing=False, stream=False, headers=None, **kwargs):
        try:
            response = requests.request(method, self.endpoint + path,
                                        headers={**self.headers, **(headers or {})},
                                        allow_redirects=False, timeout=(10, self.timeout),
                                        stream=stream, **kwargs)
            if 200 <= response.status_code < 300 and stream:
                return response
            with response:
                code = response.status_code
                if code == 404 and missing:
                    return None
                if code in (401, 403, 408, 409, 425, 429) or code >= 500:
                    raise WorkerWait(f"Worker unavailable or busy (HTTP {code}).")
                if not 200 <= code < 300:
                    raise JobError(f"Worker rejected the request (HTTP {code}).")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise JobError("Worker returned an invalid API response.")
                return payload
        except requests.exceptions.JSONDecodeError:
            raise JobError("Worker returned an invalid API response.") from None
        except requests.RequestException:
            raise WorkerWait("Worker connection interrupted. Retrying when available.") from None

    def configure_concurrency(self, value):
        """Configure render slots on the connected worker; no GPU-memory gating."""
        try:
            response = self.request("POST", "/api/config/concurrency",
                                    json={"concurrency": value})
        except JobError as exc:
            # Earlier workers are single-render only; preserve 1-job support.
            if value == 1 and "HTTP 404" in str(exc):
                return
            raise WorkerWait("Worker image does not support render concurrency 2; deploy the updated Concat worker.") from None
        if response.get("concurrency") != value:
            raise WorkerWait("Worker did not confirm the requested render concurrency.")

    def upload(self, job, path, size, digest, manifest, checkpoint, uploading, submit):
        checkpoint()
        self.request("GET", self.health)
        caps = self.request("GET", "/api/capabilities")
        # Earlier compatible CUDA workers validate manifests but do not
        # advertise their fixed 2 MiB validation limit.
        limit = caps.get("max_project_manifest_bytes", LEGACY_MANIFEST_LIMIT)
        if (caps.get("api_version") != 1 or caps.get("validation_version") != 1
                or 1 not in (caps.get("bundle_versions") or [])
                or MODE not in (caps.get("render_modes") or []) or type(limit) is not int or limit <= 0):
            raise JobError("Configured worker lacks the Concat CUDA hybrid upload and validation API.")
        if len(manifest) > limit:
            raise JobError(f"Project JSON exceeds this worker's {limit:,}-byte validation limit.")
        validation = self.request("POST", "/api/validate", data=manifest,
                                  headers={"Content-Type": "application/json"})
        if validation.get("ok") is not True:
            details = validation.get("errors") or ["Project is not CUDA compatible."]
            raise JobError(f"GPU compatibility check failed. {str(details[0])[:300]}")
        checkpoint()
        uploading()
        root = "/api/uploads/" + job
        session = self.request("POST", root, json={"size": size, "sha256": digest, "render_mode": MODE})
        chunk = session.get("chunk_size")
        count = (size + CHUNK - 1) // CHUNK
        uploaded = session.get("uploaded")
        if (session.get("render_mode") != MODE or session.get("size") != size
                or session.get("sha256") != digest or chunk != CHUNK
                or session.get("total_chunks") != count or not isinstance(uploaded, list)
                or any(type(index) is not int or not 0 <= index < count for index in uploaded)):
            raise JobError("Invalid worker upload session or chunk layout.")
        present = set(uploaded)
        with path.open("rb") as source:
            for index in range(count):
                checkpoint()
                if index in present:
                    continue
                source.seek(index * CHUNK)
                data = source.read(CHUNK)
                self.request("PUT", f"{root}/chunks/{index}", data=data, headers={
                    "Content-Type": "application/octet-stream",
                    "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
                })
        submit()
        self.request("POST", root + "/complete")


class ProductionQueue:
    def __init__(self, manager, worker_factory):
        self.media = manager
        self.drive = DriveIntake(manager.execute)
        self.worker_factory = worker_factory
        self.spool = manager.root / "production"
        if self.spool.is_symlink():
            raise RuntimeError("Cloud Render Production directory cannot be a symlink.")
        self.spool.mkdir(mode=0o700, exist_ok=True)
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = None
        self.observation_cursor = 0
        with manager.connection() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS production_jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, remote_id TEXT NOT NULL UNIQUE,
                drive_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, resource_keys TEXT NOT NULL,
                drive_size INTEGER NOT NULL, drive_md5 TEXT NOT NULL,
                folder_id TEXT NOT NULL, drive_path TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'Queued', error TEXT NOT NULL DEFAULT '',
                digest TEXT NOT NULL DEFAULT '', endpoint TEXT NOT NULL DEFAULT '',
                submitted_at INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
                rerender INTEGER NOT NULL DEFAULT 0, output_sha TEXT NOT NULL DEFAULT '',
                output_size INTEGER NOT NULL DEFAULT 0
            )""")
            con.execute("INSERT OR IGNORE INTO config VALUES('production_queue', 'stopped')")

    def start(self):
        self.stop.clear()
        self.thread = threading.Thread(target=self.run, name="cloud-render-production", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)

    def run(self):
        # The Scene web service normally runs one process. An OS lock also
        # prevents duplicate dispatch during overlapping process lifetimes.
        with (self.spool / ".dispatcher.lock").open("a") as lock:
            while not self.stop.is_set():
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.stop.wait(2)
            while not self.stop.is_set():
                try:
                    delay = self.tick()
                except Exception:
                    LOG.exception("Cloud Render Production scheduler failed")
                    delay = 8
                self.wake.wait(delay)
                self.wake.clear()

    def scan(self, link):
        try:
            candidates = self.drive.discover(link)
        except RuntimeError as exc:
            raise HTTPException(422, str(exc)) from None
        added = 0
        with self.media.lock, self.media.connection() as con:
            for drive_id, name, keys, size, md5, folder, path in candidates:
                row = con.execute("SELECT resource_keys FROM production_jobs WHERE drive_id=?", (drive_id,)).fetchone()
                if row:
                    # A rescanned direct link can supply a resource key that a
                    # folder listing did not expose, without creating a new job.
                    if keys and keys != row["resource_keys"]:
                        pairs = filter(None, (row["resource_keys"] + "," + keys).split(","))
                        merged = {pair.split("/", 1)[0]: pair for pair in pairs}
                        con.execute("UPDATE production_jobs SET resource_keys=? WHERE drive_id=?",
                                    (",".join(merged.values()), drive_id))
                    continue
                con.execute("""INSERT INTO production_jobs(
                    remote_id,drive_id,name,resource_keys,drive_size,drive_md5,folder_id,drive_path)
                    VALUES(?,?,?,?,?,?,?,?)""", (self.new_remote_id(), drive_id, name, keys, size, md5, folder, path))
                added += 1
        self.wake.set()
        return {"added": added, "duplicates": len(candidates) - added}

    @staticmethod
    def new_remote_id():
        return "production-" + uuid.uuid4().hex

    def snapshot(self):
        with self.media.connection() as con:
            items = [dict(row) for row in con.execute(
                "SELECT id,name,state,error,cancel_requested FROM production_jobs ORDER BY id")]
            concurrency = int(self.media.get(con, "render_concurrency") or "1")
            active = sum(item["state"] not in TERMINAL and item["state"] != "Queued" for item in items)
            return {"state": self.media.get(con, "production_queue"),
                    "concurrency": concurrency, "active": active, "items": items}

    def control(self, action):
        state = {"start": "running", "pause": "paused", "resume": "running"}.get(action)
        if state is None:
            raise HTTPException(404, "Unknown queue action.")
        with self.media.lock, self.media.connection() as con:
            self.media.put(con, "production_queue", state)
            if state == "running" and self.media.get(con, "endpoint") and self.media.get(con, "worker_key"):
                self.media.put(con, "enabled", "1")
        self.wake.set()
        return {"state": state}

    def action(self, job, action):
        with self.media.lock, self.media.connection() as con:
            row = con.execute("SELECT * FROM production_jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                raise HTTPException(404, "Project not found.")
            if action == "cancel" and row["state"] not in TERMINAL:
                if row["state"] == "Queued":
                    con.execute("UPDATE production_jobs SET state='Cancelled',error='' WHERE id=?", (job,))
                else:
                    con.execute("UPDATE production_jobs SET cancel_requested=1,error='' WHERE id=?", (job,))
            elif action == "retry" and row["state"] == "Failed":
                if row["rerender"]:
                    con.execute("""UPDATE production_jobs SET remote_id=?,endpoint='',submitted_at=0,rerender=0
                                   WHERE id=?""", (self.new_remote_id(), job))
                con.execute("UPDATE production_jobs SET state='Queued',error='',cancel_requested=0 WHERE id=?", (job,))
            elif action == "delete" and row["state"] in (*TERMINAL, "Queued"):
                self.clean_input(job)
                self.output_part(job).unlink(missing_ok=True)
                con.execute("DELETE FROM production_jobs WHERE id=?", (job,))
            else:
                raise HTTPException(409, "This action is unavailable in the current project state.")
        self.wake.set()
        return {"ok": True}

    def update(self, job, *, state, error="", **values):
        fields = {"state": state, "error": error, **values}
        assignment = ",".join(key + "=?" for key in fields)
        with self.media.lock, self.media.connection() as con:
            con.execute("UPDATE production_jobs SET " + assignment + " WHERE id=?", (*fields.values(), job))

    def archive(self, job):
        return self.spool / f"{job}.zip"

    def output_part(self, job):
        return self.spool / f"{job}.output.part"

    def clean_input(self, job):
        self.archive(job).unlink(missing_ok=True)
        self.archive(job).with_suffix(".part").unlink(missing_ok=True)
        directory = self.archive(job).with_suffix(".download")
        if directory.exists():
            shutil.rmtree(directory)

    def tick(self):
        with self.media.lock, self.media.connection() as con:
            # Nonterminal jobs occupy queue slots, including GPU rendering,
            # download, retry and output delivery. No VRAM/CPU checks.
            active = con.execute("""SELECT * FROM production_jobs
                WHERE state NOT IN ('Completed','Failed','Cancelled','Queued')
                ORDER BY id""").fetchall()
            limit = int(self.media.get(con, "render_concurrency") or "1")
            row = None
            if len(active) < limit and self.media.get(con, "production_queue") == "running":
                row = con.execute("SELECT * FROM production_jobs WHERE state='Queued' ORDER BY id LIMIT 1").fetchone()
                if row is not None:
                    con.execute("UPDATE production_jobs SET state='Downloading' WHERE id=?", (row["id"],))
            if row is None and active:
                row = active[self.observation_cursor % len(active)]
                self.observation_cursor += 1
        if row is None:
            # A crash between recording completion and unlinking the input is
            # safe. Reap only files whose jobs are durably completed.
            for path in self.spool.glob("*.zip"):
                if path.stem.isascii() and path.stem.isdigit():
                    with self.media.connection() as con:
                        done = con.execute("SELECT 1 FROM production_jobs WHERE id=? AND state='Completed'",
                                           (int(path.stem),)).fetchone()
                    if done:
                        self.clean_input(int(path.stem))
            return 8
        try:
            self.process(row)
        except Checkpoint:
            return 2
        except WorkerWait as exc:
            self.update(row["id"], state="Waiting for Worker", error=str(exc)[:350])
        except (JobError, RuntimeError, HTTPException) as exc:
            self.update(row["id"], state="Failed", error=str(getattr(exc, "detail", str(exc)))[:350])
            return 0
        except OSError:
            LOG.exception("Cloud Render Production local I/O failed")
            self.update(row["id"], state="Failed", error="VPS file operation failed. Check storage and retry.")
            return 0
        return 5

    def checkpoint(self, job, *, submit=False):
        if self.stop.is_set():
            raise Checkpoint()
        with self.media.lock, self.media.connection() as con:
            row = con.execute("SELECT cancel_requested FROM production_jobs WHERE id=?", (job,)).fetchone()
            if row["cancel_requested"] or self.media.get(con, "production_queue") != "running":
                raise Checkpoint()
            if submit:
                con.execute("UPDATE production_jobs SET submitted_at=? WHERE id=?", (int(time.time()), job))

    def process(self, row):
        job, remote_id = row["id"], row["remote_id"]
        if row["cancel_requested"] and not row["submitted_at"]:
            self.update(job, state="Cancelled")
            return
        if self.media.production_received(remote_id):
            self.finish(job)
            return
        if not row["submitted_at"]:
            self.checkpoint(job)

        worker = self.worker_factory(row["endpoint"])
        with self.media.connection() as con:
            concurrency = int(self.media.get(con, "render_concurrency") or "1")
        worker.configure_concurrency(concurrency)
        # Check before uploading or re-uploading. The same worker ID and
        # content checksum survive web restarts and ambiguous POST responses.
        if row["endpoint"]:
            status = worker.request("GET", "/api/jobs/" + remote_id, missing=True)
            if status is not None:
                if status.get("sha256") != row["digest"] or status.get("render_mode") != MODE:
                    raise JobError("Worker job identity does not match the saved Concat ZIP.")
                self.observe(row, worker, status)
                return
        if row["cancel_requested"]:
            if time.time() - row["submitted_at"] >= max(30, worker.timeout):
                self.update(job, state="Cancelled")
            return

        self.checkpoint(job)
        path = self.archive(job)
        if not path.exists():
            self.update(job, state="Downloading")
            part = path.with_suffix(".part")
            part.unlink(missing_ok=True)
            name = self.drive.download(row, part)
            self.update(job, state="Downloading", name=name)
            self.checkpoint(job)
            if not part.is_file() or not 0 < part.stat().st_size <= MAX_ZIP:
                raise JobError("Drive download is empty or exceeds the Concat ZIP size limit.")
            if row["drive_size"] > 0 and part.stat().st_size != row["drive_size"]:
                raise JobError("Drive ZIP size changed after the scan. Delete the job and scan again.")
            if row["drive_md5"]:
                with part.open("rb") as stream:
                    if hashlib.file_digest(stream, "md5").hexdigest() != row["drive_md5"]:
                        raise JobError("Drive ZIP content changed after the scan. Delete the job and scan again.")
            manifest = bundle_manifest(part)
            os.replace(part, path)
        else:
            manifest = bundle_manifest(path)
        digest = row["digest"]
        if not digest:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        with self.media.connection() as con:
            same = con.execute("""SELECT 1 FROM production_jobs WHERE digest=? AND id<>?
                AND state NOT IN ('Failed','Cancelled')""", (digest, job)).fetchone()
        if same:
            raise JobError("An identical Concat ZIP is already in Production.")
        self.update(job, state="Waiting for Worker", digest=digest)
        worker.upload(remote_id, path, path.stat().st_size, digest, manifest,
                      lambda: self.checkpoint(job),
                      lambda: self.update(job, state="Uploading to GPU", endpoint=worker.endpoint),
                      lambda: self.checkpoint(job, submit=True))
        self.update(job, state="Rendering")

    def observe(self, row, worker, status):
        job, remote_id = row["id"], row["remote_id"]
        state = status.get("state")
        if state in ("queued", "running", "delivering"):
            self.update(job, state="Delivering" if state == "delivering" else "Rendering")
            return
        if row["cancel_requested"]:
            self.update(job, state="Cancelled")
            return
        if state == "failed":
            error = str(status.get("error") or "GPU render failed. Check the Salad worker logs.")
            self.update(job, state="Failed", error=error[:350], rerender=1)
        elif state in ("rendered", "delivery_failed"):
            self.update(job, state="Delivering")
            self.pull(row, worker, status)
            self.finish(job)
        elif state == "delivered":
            if not self.media.production_received(remote_id):
                raise JobError("Worker reports delivery, but the verified MP4 is missing from Cloud Render Media.")
            self.finish(job)
        else:
            raise JobError("Worker returned an unknown render state.")

    def pull(self, row, worker, status):
        remote_id, job = row["remote_id"], row["id"]
        size, sha = status.get("output_size"), status.get("output_sha256")
        if type(size) is not int or not 12 <= size <= 20 * 1024 ** 3 or not SHA256.fullmatch(str(sha)):
            raise JobError("Worker did not return a valid MP4 size and checksum.")
        path = self.output_part(job)
        if shutil.disk_usage(path.parent).free < size + 5_000_000_000:
            raise JobError("Insufficient VPS disk reserve for the rendered MP4.")
        digest, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
        length = 0
        try:
            response = worker.request("GET", "/api/jobs/" + remote_id + "/output", stream=True, missing=True)
            if response is None:
                raise WorkerWait("Waiting for the worker to make the MP4 available.")
            with response:
                with path.open("wb") as stream:
                    for chunk in response.iter_content(CHUNK):
                        if self.stop.is_set():
                            raise Checkpoint()
                        length += len(chunk)
                        if length > size:
                            raise JobError("Worker MP4 exceeded its declared size.")
                        digest.update(chunk)
                        md5.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
        except requests.RequestException:
            # A push can start while the worker still reports 'rendered'.
            raise WorkerWait("Waiting for the worker to make the MP4 available.") from None
        if length != size or digest.hexdigest() != sha:
            raise JobError("Worker MP4 size or SHA256 verification failed.")
        with path.open("rb") as stream:
            if stream.read(12)[4:8] != b"ftyp":
                raise JobError("Worker output does not have a valid MP4 signature.")
        self.media.publish_production(remote_id, Path(row["name"]).stem[:135] + ".mp4",
                                      path, size, sha, md5.hexdigest())

    def finish(self, job):
        with self.media.lock, self.media.connection() as con:
            row = con.execute("SELECT cancel_requested,output_sha FROM production_jobs WHERE id=?", (job,)).fetchone()
            if not row["output_sha"]:
                raise JobError("Verified MP4 delivery was not recorded.")
            state = "Cancelled" if row["cancel_requested"] else "Completed"
            con.execute("UPDATE production_jobs SET state=?,error='' WHERE id=?", (state, job))
        if state == "Completed":
            self.clean_input(job)
        self.output_part(job).unlink(missing_ok=True)
