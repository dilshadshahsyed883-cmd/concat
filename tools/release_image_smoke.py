"""Run inside the built image. No NVIDIA execution is claimed on CI runners."""
import hashlib
import http.client
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import unittest
import wave

from cloud_render.concat.gpu.worker import Handler, RenderWorker, SaladDualStackHTTPServer
from cloud_render.concat.gpu.caption_jobs import preset_template
from cloud_render.concat.hybrid.contract import RENDER_CONTRACT_ID, RENDER_CONTRACT_VERSION

expected = os.environ['EXPECTED_SOURCE_SHA']
assert os.environ['CONCAT_SOURCE_SHA'] == expected
# Prove tests use the exact application sources actually copied into the image.
for folder in ('hybrid', 'gpu'):
    for installed in (Path('/opt/app/cloud_render/concat') / folder).rglob('*.py'):
        relative = installed.relative_to('/opt/app')
        assert hashlib.sha256(installed.read_bytes()).digest() == hashlib.sha256((Path('/src') / relative).read_bytes()).digest(), str(relative)
for name in ('classic-subtitle', 'bold-highlight', 'cinematic-title', 'minimal-clean',
             'news-alert', 'modern-accent', 'karaoke-highlight', 'preset-1-purple-word', 'preset-2-yellow-bar'):
    assert preset_template('cuda-caption.' + name)
models = sorted(str(p.relative_to('/worker/asr-models')) for p in Path('/worker/asr-models').rglob('model.bin'))
assert models, 'default small ASR model weights missing'
versions = {name: importlib.metadata.version(name) for name in ('faster-whisper', 'av', 'ctranslate2', 'cupy-cuda12x', 'PyNvVideoCodec', 'Pillow')}
assert versions['av'] == '18.1.0', 'GPU image has incompatible PyAV version: ' + versions['av']
# Exercise the exact decode path used by faster-whisper before publishing.
# Import/model startup alone cannot detect av.open(metadata_errors=...) regressions.
from faster_whisper.audio import decode_audio
with tempfile.TemporaryDirectory() as audio_probe:
    wav_path = Path(audio_probe) / 'asr-compat.wav'
    with wave.open(str(wav_path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b'\x00\x00' * 16000)
    samples = decode_audio(str(wav_path), sampling_rate=16000)
    assert len(samples) >= 4000, 'faster-whisper failed to decode the test WAV'
with tempfile.TemporaryDirectory() as work:
    token = secrets.token_hex(32)
    worker = RenderWorker(Path(work), token=token)
    server = SaladDualStackHTTPServer(('::1', 0), Handler)
    server.worker = worker
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection('::1', server.server_address[1], timeout=20)
        conn.request('GET', '/health/live')
        response = conn.getresponse(); assert response.status == 200; response.read()
        conn.request('GET', '/api/capabilities')
        response = conn.getresponse(); assert response.status == 401; response.read()
        conn.close()  # Authentication errors deliberately close the worker connection.
        conn.request('GET', '/api/capabilities', headers={'Authorization': 'Bearer ' + token})
        response = conn.getresponse(); assert response.status == 200
        capabilities = json.loads(response.read())
        assert capabilities['source_sha'] == expected
        assert capabilities['render_contract'] == {'id': RENDER_CONTRACT_ID, 'version': RENDER_CONTRACT_VERSION}
        assert capabilities['gpu_captions']['available'] is False, 'CI smoke must not claim an untested NVIDIA device'
        conn.close()
    finally:
        server.shutdown(); server.server_close(); worker.pool.shutdown(wait=True); thread.join()
# Add only the test source namespace; application modules above came from /opt/app.
sys.path.append('/src')
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'cloud_render.concat.tests.test_render_contract',
    'cloud_render.concat.tests.test_asr_pyav_compat',
    'cloud_render.concat.tests.test_asr_timestamp_types',
    'cloud_render.concat.tests.test_gpu_asr_alignment',
    'cloud_render.concat.tests.test_gpu_karaoke_timing',
    'cloud_render.concat.tests.test_gpu_png_support',
    'cloud_render.concat.tests.test_gpu_caption_jobs',
    'cloud_render.concat.tests.test_gpu_compat_validation',
])
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
assert result.wasSuccessful(), 'image contract regressions failed'
print(json.dumps({'source_sha': expected, 'render_contract': capabilities['render_contract'],
    'worker_startup': 'passed', 'authenticated_capabilities': 'passed', 'presets': 9,
    'asr_model_assets': models, 'asr_decoder_smoke': 'passed', 'runtime_versions': versions,
    'tests_run': result.testsRun, 'hardware_skips': len(result.skipped),
    'physical_gpu_verified': False}, indent=2))
