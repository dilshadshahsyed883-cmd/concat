#!/usr/bin/env python3
"""Offline Concat project/media transfer. Never uploads or changes source files."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

PROJECT_NAMES = ("concat.json", "wolfcut.json")
SAFE_MEDIA = re.compile(r"media/[a-f0-9]{32}(?:\.[a-z0-9]{1,12})?")
LIMIT = 500 * 1024 ** 3
# A bounded JSON budget permits large timelines without unbounded ZIP expansion.
MAX_PROJECT_MANIFEST_BYTES = 64 * 1024 * 1024


class TransferError(ValueError):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def _load_project(root: Path):
    for filename in PROJECT_NAMES:
        file = root / filename
        if file.is_file():
            size = file.stat().st_size
            if not 0 < size <= MAX_PROJECT_MANIFEST_BYTES:
                raise TransferError(f"Concat project JSON must be 1-{MAX_PROJECT_MANIFEST_BYTES} bytes (got {size})")
            doc = json.loads(file.read_text(encoding="utf-8-sig"))
            if not isinstance(doc, dict) or not isinstance(doc.get("media", []), list):
                raise TransferError("Invalid Concat project media list")
            return filename, doc
    raise TransferError("No concat.json or legacy wolfcut.json found")


def pack(root: Path, destination: Path) -> dict:
    root = root.expanduser().resolve(strict=True)
    destination = destination.expanduser().absolute()
    if not root.is_dir() or destination.exists():
        raise TransferError("Source must be a directory; destination ZIP must be new")
    manifest_name, project = _load_project(root)
    media = project.get("media", [])
    if len(media) > 20000:
        raise TransferError("Too many media entries")
    seen = {}
    references = []
    total = 0
    for index, entry in enumerate(media):
        if not isinstance(entry, dict):
            raise TransferError("Invalid media record")
        raw = entry.get("path", "")
        if not raw:
            continue  # Empty placeholders can be filled later.
        if not isinstance(raw, str):
            raise TransferError("Invalid media path")
        source = Path(raw).expanduser()
        source = (source if source.is_absolute() else root / source).resolve(strict=True)
        if not source.is_file():
            raise TransferError("Missing media file")
        identity = str(source)
        if identity not in seen:
            suffix = source.suffix.lower().lstrip(".")
            suffix = "." + suffix if re.fullmatch(r"[a-z0-9]{1,12}", suffix) else ""
            member = "media/" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32] + suffix
            size = source.stat().st_size
            total += size
            if total > LIMIT:
                raise TransferError("Too many media bytes for one archive")
            seen[identity] = (member, source, sha256(source), size)
        member, _, digest, size = seen[identity]
        references.append({"index": index, "member": member, "sha256": digest, "size": size})
    control = {"version": 1, "project_file": manifest_name, "references": references}
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(destination, "x", allowZip64=True) as bundle:
            bundle.writestr("bundle.json", json.dumps(control, sort_keys=True))
            bundle.write(root / manifest_name, "project/" + manifest_name, compress_type=zipfile.ZIP_DEFLATED)
            for member, source, _, _ in seen.values():
                bundle.write(source, member, compress_type=zipfile.ZIP_STORED)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {"files": len(seen), "references": len(references), "bytes": total}


def restore(archive: Path, destination: Path) -> dict:
    destination = destination.expanduser().absolute()
    if destination.exists():
        raise TransferError("Destination already exists; will not overwrite")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".concat-transfer-", dir=destination.parent))
    try:
        with zipfile.ZipFile(archive, "r") as bundle:
            infos = bundle.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or "bundle.json" not in names:
                raise TransferError("Duplicate or missing archive entry")
            if sum(info.file_size for info in infos) > LIMIT:
                raise TransferError("Archive is too large to extract")
            if any(stat.S_ISLNK(info.external_attr >> 16) for info in infos):
                raise TransferError("Archive contains a symlink")
            control = json.loads(bundle.read("bundle.json"))
            if control.get("version") != 1 or control.get("project_file") not in PROJECT_NAMES:
                raise TransferError("Unsupported bundle format")
            project_name = control["project_file"]
            refs = control.get("references")
            if not isinstance(refs, list) or len(refs) > 20000:
                raise TransferError("Invalid references")
            members = {}
            for item in refs:
                if not isinstance(item, dict):
                    raise TransferError("Invalid reference")
                member = item.get("member")
                if not isinstance(member, str) or not SAFE_MEDIA.fullmatch(member):
                    raise TransferError("Unsafe media path")
                if type(item.get("index")) is not int or type(item.get("size")) is not int:
                    raise TransferError("Invalid reference index or size")
                digest = item.get("sha256")
                if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise TransferError("Invalid checksum")
                if item["size"] < 0 or (member in members and members[member] != (digest, item["size"])):
                    raise TransferError("Conflicting media reference")
                members[member] = (digest, item["size"])
            original_entry = "project/" + project_name
            if set(names) != {"bundle.json", original_entry, *members}:
                raise TransferError("Archive has missing or unexpected files")
            project_bytes = bundle.getinfo(original_entry).file_size
            if not 0 < project_bytes <= MAX_PROJECT_MANIFEST_BYTES:
                raise TransferError(f"Packaged Concat project JSON is {project_bytes} bytes; maximum {MAX_PROJECT_MANIFEST_BYTES} bytes")
            original = bundle.read(original_entry)
            project = json.loads(original.decode("utf-8-sig"))
            if not isinstance(project, dict) or not isinstance(project.get("media", []), list):
                raise TransferError("Invalid project document")
            for ref in refs:
                index = ref["index"]
                if index < 0 or index >= len(project["media"]) or not isinstance(project["media"][index], dict):
                    raise TransferError("Invalid media index")
                project["media"][index]["path"] = str(destination / ref["member"])
            for member, (digest, size) in members.items():
                target = staging / member
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                if target.stat().st_size != size or sha256(target) != digest:
                    raise TransferError("Media checksum mismatch")
            (staging / (project_name + ".original")).write_bytes(original)
            (staging / project_name).write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.rename(destination)
        return {"files": len(members), "references": len(refs), "project": str(destination / project_name)}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pack_cmd = sub.add_parser("pack", help="Archive PC project and media")
    pack_cmd.add_argument("source", type=Path)
    pack_cmd.add_argument("destination", type=Path)
    restore_cmd = sub.add_parser("restore", help="Extract on Linux and relink media")
    restore_cmd.add_argument("archive", type=Path)
    restore_cmd.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = pack(args.source, args.destination) if args.command == "pack" else restore(args.archive, args.destination)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
