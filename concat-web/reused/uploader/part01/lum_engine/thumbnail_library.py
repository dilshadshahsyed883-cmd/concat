"""Owner-scoped thumbnail originals with atomic queue references and safe cleanup."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import uuid
import warnings

from PIL import Image
from desktop_core.modules.thumbnail_limits import MAX_THUMBNAIL_BYTES
from .config import THUMBNAIL_ROOT
from .project_store import now_iso


class ThumbnailLibrary:
    def __init__(self, store):
        self.store = store
        self.root = store.path.parent / "thumbnail-library"

    def folder(self, owner, *, create=False):
        if not isinstance(owner, str) or not owner or owner != owner.strip():
            raise ValueError("Verified owner is required")
        path = self.root / hashlib.sha256(owner.encode()).hexdigest()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Unsafe thumbnail storage")
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def path(self, row, owner):
        if not re.fullmatch(r"[a-f0-9]{32}", str(row["id"])) or row["suffix"] not in {".jpg", ".jpeg", ".png"}:
            raise ValueError("Invalid thumbnail identity")
        return self.folder(owner) / (row["id"] + row["suffix"])

    def row(self, conn, owner, identity):
        row = conn.execute("SELECT * FROM concat_thumbnails WHERE id=? AND owner_username=? COLLATE NOCASE", (identity, owner)).fetchone()
        if not row:
            raise ValueError("Thumbnail is unavailable in your library")
        return row

    def read(self, row, owner):
        path = self.path(row, owner)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            import stat
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != row["size"] or info.st_size > MAX_THUMBNAIL_BYTES:
                raise ValueError("Thumbnail changed; select it again")
            payload = stream.read(MAX_THUMBNAIL_BYTES + 1)
        if hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise ValueError("Thumbnail checksum changed")
        return payload

    def used(self, conn, owner, identity, *, exclude=0):
        return bool(conn.execute("""SELECT 1 FROM concat_thumbnail_uses u JOIN projects p ON p.id=u.project_id
            WHERE u.thumbnail_id=? AND p.owner_username=? COLLATE NOCASE AND p.id!=?
              AND p.status NOT IN ('COMPLETED','CANCELLED')
              AND NOT (COALESCE(p.remote_verified_at,'')!='' AND u.applied_at!='') LIMIT 1""", (identity, owner, exclude)).fetchone())

    def items(self, owner):
        with self.store.connect() as conn:
            rows = conn.execute("SELECT * FROM concat_thumbnails WHERE owner_username=? COLLATE NOCASE ORDER BY created_at DESC,id LIMIT 1000", (owner,)).fetchall()
            result = []
            for row in rows:
                try:
                    path = self.path(row, owner)
                    if path.is_symlink() or not path.is_file() or path.stat().st_size != row["size"]:
                        continue
                except (ValueError, OSError):
                    continue
                result.append({"id": row["id"], "name": row["name"], "sha256": row["sha256"],
                               "size": row["size"], "in_use": self.used(conn, owner, row["id"])})
            return result

    def add(self, owner, name, payload, *, replace=False):
        name = str(name or "").strip()
        suffix = Path(name).suffix.lower()
        if not name or len(name) > 200 or any(c in name for c in "/\\") or any(ord(c) < 32 for c in name) or suffix not in {".jpg", ".jpeg", ".png"}:
            raise ValueError("Use a JPG/PNG filename without paths (up to 200 characters)")
        if not 0 < len(payload) <= MAX_THUMBNAIL_BYTES:
            raise ValueError("Thumbnail must be nonempty and at most 50 MB")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as image:
                    expected = "PNG" if suffix == ".png" else "JPEG"
                    if image.format != expected or image.width * image.height > 40_000_000:
                        raise ValueError("Image format or dimensions are invalid")
                    image.verify()
                with Image.open(io.BytesIO(payload)) as image:
                    image.load()
        except Exception as exc:
            raise ValueError("Invalid or damaged JPG/PNG image (maximum 40 megapixels)") from exc
        folder = self.folder(owner, create=True)
        if shutil.disk_usage(folder).free < len(payload) + 100 * 1024 * 1024:
            raise ValueError("Not enough thumbnail storage space")
        identity = uuid.uuid4().hex
        row = dict(id=identity, suffix=suffix)
        path = self.path(row, owner)
        created = False
        old_path = None
        try:
            with self.store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                old = conn.execute("SELECT * FROM concat_thumbnails WHERE owner_username=? COLLATE NOCASE AND name=? COLLATE NOCASE", (owner, name)).fetchone()
                if old and (not replace or self.used(conn, owner, old["id"])):
                    raise ValueError("Filename already exists; choose Replace matching names, or wait until its queue entries finish")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                created = True
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                if old:
                    old_path = self.path(old, owner)
                    if old_path.is_symlink():
                        raise ValueError("Unsafe existing thumbnail path")
                    conn.execute("DELETE FROM concat_thumbnails WHERE id=?", (old["id"],))
                conn.execute("INSERT INTO concat_thumbnails VALUES(?,?,?,?,?,?,?)", (identity, owner, name, hashlib.sha256(payload).hexdigest(), len(payload), suffix, now_iso()))
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise
        if old_path:
            # Commit the new identity before removing the old file. A crash
            # can leave an unindexed old blob, but cannot lose the new image.
            try:
                if old_path.is_symlink():
                    raise OSError("Unsafe old thumbnail path")
                old_path.unlink(missing_ok=True)
            except OSError:
                self.store.event(None, "Old thumbnail file retained after replacement; storage review required", "WARNING")
        return identity

    def stage(self, owner, identity):
        with self.store.connect() as conn:
            row = self.row(conn, owner, identity)
            payload = self.read(row, owner)
        THUMBNAIL_ROOT.mkdir(parents=True, exist_ok=True)
        if any(p.is_symlink() for p in (THUMBNAIL_ROOT, *THUMBNAIL_ROOT.parents)):
            raise ValueError("Unsafe staged thumbnail storage")
        path = THUMBNAIL_ROOT / ("library_" + uuid.uuid4().hex + row["suffix"])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return str(path), row["sha256"]

    def delete(self, owner, identity, *, project_id=0, fingerprint=""):
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM concat_thumbnails WHERE id=? AND owner_username=? COLLATE NOCASE", (identity, owner)).fetchone()
            if not row:
                return False
            if project_id:
                receipt = conn.execute("""SELECT p.remote_verified_at,p.youtube_video_id,u.applied_at,u.sha256
                    FROM projects p JOIN concat_thumbnail_uses u ON u.project_id=p.id
                    WHERE p.id=? AND p.owner_username=? COLLATE NOCASE AND u.thumbnail_id=?""", (project_id, owner, identity)).fetchone()
                if not receipt or not all(receipt[k] for k in ("remote_verified_at", "youtube_video_id", "applied_at")) or receipt["sha256"] != fingerprint or row["sha256"] != fingerprint:
                    raise ValueError("Verified video and successful thumbnail receipt are required")
            if self.used(conn, owner, identity, exclude=project_id):
                raise ValueError("Thumbnail is in use by another queue entry")
            path = self.path(row, owner)
            if path.exists() or path.is_symlink():
                self.read(row, owner)  # refuses changed bytes and symlinks before unlink
                path.unlink()
            conn.execute("DELETE FROM concat_thumbnails WHERE id=?", (identity,))
        return True

    def match(self, owner, video_name):
        base = re.sub(r"\.mp4$", "", str(video_name).strip(), flags=re.I).casefold()
        matches = [item for item in self.items(owner) if Path(item["name"]).stem.casefold() == base]
        if len(matches) == 1:
            return matches[0], "Matching library thumbnail selected"
        return None, "Multiple matching thumbnails; choose one in Thumbnail Library" if matches else "No matching thumbnail; choose one manually or add it to Thumbnail Library"

    def use(self, project_id):
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM concat_thumbnail_uses WHERE project_id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def mark_applied(self, project_id):
        with self.store.connect() as conn:
            conn.execute("UPDATE concat_thumbnail_uses SET applied_at=? WHERE project_id=?", (now_iso(), project_id))
