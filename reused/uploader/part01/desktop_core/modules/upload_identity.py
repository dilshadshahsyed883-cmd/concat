from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .path_utils import resolve_stored_path

FINGERPRINT_VERSION = 2
_HASH_CHUNK_SIZE = 4 * 1024 * 1024


class DuplicateUploadError(RuntimeError):
    """Raised when an equivalent upload is already reserved or completed."""


def normalized_media_path(value: Any) -> str:
    path = resolve_stored_path(str(value or ""))
    try:
        resolved = path.resolve(strict=False)
    except Exception:
        resolved = path.absolute()
    return os.path.normcase(os.path.normpath(str(resolved)))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def canonical_upload_payload(data: Mapping[str, Any], media_sha256: str) -> dict[str, Any]:
    """Return the strict identity for one channel/media pair.

    A title, description, schedule, privacy, thumbnail, or playlist edit must not
    create a second copy of identical media on the same YouTube channel. The
    channel is part of the identity so the same asset can still be uploaded to
    different channels. Byte-identical files at different paths are equivalent.
    """

    return {
        "version": FINGERPRINT_VERSION,
        "channel_db_id": int(data.get("channel_db_id") or 0),
        "media_sha256": str(media_sha256),
    }


def upload_fingerprint(data: Mapping[str, Any], media_sha256: str) -> str:
    encoded = json.dumps(
        canonical_upload_payload(data, media_sha256),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
