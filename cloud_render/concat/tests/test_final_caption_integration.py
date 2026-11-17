"""Real Rust document -> ZIP -> caption engine -> CUDA planner handoff.

ASR is deterministic/injected; this does not assert NVIDIA execution or MP4 output.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
import wave

from cloud_render.concat.gpu.caption_jobs import CaptionEngine, resolve_request
from cloud_render.concat.hybrid.project import _caption
from cloud_render.concat.project_bundle import pack, restore
from cloud_render.concat.tests.test_gpu_caption_jobs import FakeCuda, TIMED


class FinalCaptionIntegration(unittest.TestCase):
    def test_nine_native_saved_requests_survive_zip_asr_cache_and_karaoke(self):
        directory = os.environ.get('CONCAT_CAPTION_FIXTURE_DIR')
        if not directory:
            self.skipTest('Rust fixtures are required and generated in combined source CI')
        fixtures = sorted(Path(directory).glob('*.json'))
        self.assertEqual(len(fixtures), 9)
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            backend = FakeCuda()
            engine = CaptionEngine(base / 'cache', backend)
            for index, fixture in enumerate(fixtures):
                with self.subTest(preset=fixture.stem):
                    desktop = base / f'desktop-{index}'
                    desktop.mkdir()
                    audio = desktop / 'narration.wav'
                    with wave.open(str(audio), 'wb') as stream:
                        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                        stream.writeframes(b'\0\0' * 32000)
                    document = json.loads(fixture.read_text())
                    document['media'] = [{'id': 'narration', 'path': str(audio), 'kind': 'audio'}]
                    voice = document['timelines'][0]['clips'][0]
                    voice['mediaId'] = 'narration'
                    self.assertFalse(any(c['kind'] == 'text' for c in document['timelines'][0]['clips']))
                    manual = {'id': 'manual', 'kind': 'text', 'trackId': voice['trackId'],
                              'start': 0, 'duration': 1, 'offsetY': -.2,
                              'text': {'content': 'User authored'}}
                    document['timelines'][0]['clips'].append(manual)
                    manifest = desktop / 'concat.json'
                    manifest.write_text(json.dumps(document))
                    original = manifest.read_bytes()
                    bundle = base / f'{index}.zip'
                    pack(desktop, bundle)
                    worker = base / f'worker-{index}'
                    restore(bundle, worker)
                    restored = json.loads((worker / 'concat.json').read_text())
                    self.assertEqual(restored['captionPreset'], document['captionPreset'])
                    self.assertEqual(resolve_request(restored), resolve_request(document))
                    result = engine.run(worker, base / f'artifacts-{index}')
                    self.assertTrue(result['render_after'])
                    self.assertEqual(result['result']['cacheHit'], index > 0)
                    generated_doc = json.loads((worker / 'concat.json').read_text())
                    self.assertIn(manual, generated_doc['timelines'][0]['clips'])
                    generated = [c for c in generated_doc['timelines'][0]['clips'] if c.get('gpuCaptionSource')]
                    self.assertEqual(len(generated), 1)
                    clip = generated[0]
                    self.assertEqual(clip['text']['color'], '#123456')
                    self.assertAlmostEqual(clip['offsetY'], .35)
                    self.assertEqual(clip['captionWords'][1]['startMs'], 500)
                    # Feed the emitted native text into the actual CUDA caption planner.
                    overlay = _caption(clip, 3, 57, 1, 1)
                    self.assertIsNotNone(overlay)
                    self.assertEqual(manifest.read_bytes(), original)
                    self.assertEqual(result['result']['cues'], TIMED['cues'])
            self.assertEqual(backend.calls, 1, 'style-only changes must reuse ASR')


if __name__ == '__main__':
    unittest.main()
