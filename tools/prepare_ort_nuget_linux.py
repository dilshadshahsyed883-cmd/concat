"""Stage pinned official ONNX Runtime 1.28.0 Linux x64 native SDK from NuGet.

The archive is also SHA-256 checked by upstream/scripts/ci/fetch.sh.
Do not substitute an unpinned version or run any code from the NuGet package.
"""
import hashlib
from pathlib import Path, PurePosixPath
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile

if len(sys.argv) != 3:
    raise SystemExit("usage: prepare_ort_nuget_linux.py ARCHIVE DESTINATION")

archive, destination = map(Path, sys.argv[1:])
expected_sha = "769d1d3ea8ab6cd69f737c9dd4d4462aa4ad0ccfa106eaf506efc40d7bead5db"
digest = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
assert digest == expected_sha, "ONNX NuGet archive SHA-256 mismatch"

with zipfile.ZipFile(archive) as package:
    spec = ET.fromstring(package.read("Microsoft.ML.OnnxRuntime.nuspec"))
    metadata = {e.tag.rsplit("}", 1)[-1]: e.text for e in spec.iter()}
    assert metadata["id"] == "Microsoft.ML.OnnxRuntime"
    assert metadata["version"] == "1.28.0"

    lib = package.read("runtimes/linux-x64/native/libonnxruntime.so")
    assert lib[:4] == b"\x7fELF", "ONNX native library is not ELF"
    assert lib[4:6] == b"\x02\x01", "ONNX native library is not 64-bit little endian"
    assert struct.unpack_from("<H", lib, 18)[0] == 62, "ONNX native library is not Linux x86-64"

    lib_dir = destination / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    soname = "libonnxruntime.so.1.28.0"
    (lib_dir / soname).write_bytes(lib)
    for link in ("libonnxruntime.so", "libonnxruntime.so.1"):
        path = lib_dir / link
        if path.is_symlink() or path.exists():
            path.unlink()
        path.symlink_to(soname)

    prefix = "build/native/include/"
    header_count = 0
    for name in package.namelist():
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        relative = PurePosixPath(name[len(prefix):])
        assert relative.parts and not relative.is_absolute() and ".." not in relative.parts
        target = destination / "include" / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(package.read(name))
        header_count += 1
    assert header_count and (destination / "include" / "onnxruntime_c_api.h").is_file()

print("Microsoft ONNX Runtime 1.28.0 Linux x64 NuGet SDK verified (SHA-256, ELF x86-64, headers)")
