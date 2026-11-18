"""Stage Microsoft's pinned ONNX 1.28.0 native Windows SDK from its NuGet.

fetch.sh verifies the full archive SHA-256 before this script is called.
Only the declared native SDK members are copied; no .NET code is executed.
"""
import hashlib
from pathlib import Path, PurePosixPath
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile

archive, destination = map(Path, sys.argv[1:])
expected = '769d1d3ea8ab6cd69f737c9dd4d4462aa4ad0ccfa106eaf506efc40d7bead5db'
assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected, 'ONNX NuGet integrity mismatch'
with zipfile.ZipFile(archive) as package:
    spec = ET.fromstring(package.read('Microsoft.ML.OnnxRuntime.nuspec'))
    values = {e.tag.rsplit('}', 1)[-1]: e.text for e in spec.iter()}
    assert values['id'] == 'Microsoft.ML.OnnxRuntime' and values['version'] == '1.28.0'
    dll = package.read('runtimes/win-x64/native/onnxruntime.dll')
    pe = struct.unpack_from('<I', dll, 0x3c)[0]
    assert dll[:2] == b'MZ' and dll[pe:pe+4] == b'PE\0\0'
    assert struct.unpack_from('<H', dll, pe+4)[0] == 0x8664, 'ONNX DLL is not Windows x64'
    for name in ('onnxruntime.dll', 'onnxruntime.lib', 'onnxruntime_providers_shared.dll', 'onnxruntime_providers_shared.lib'):
        out = destination / 'lib' / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(package.read('runtimes/win-x64/native/' + name))
    headers = 0
    prefix = 'build/native/include/'
    for name in package.namelist():
        if name.startswith(prefix) and not name.endswith('/'):
            relative = PurePosixPath(name[len(prefix):])
            assert not relative.is_absolute() and '..' not in relative.parts
            out = destination / 'include' / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(package.read(name)); headers += 1
    assert headers and (destination / 'include/onnxruntime_c_api.h').is_file()
print('Verified Microsoft ONNX Runtime 1.28.0 Windows x64 native DLL/import library/header SDK from pinned official NuGet')
