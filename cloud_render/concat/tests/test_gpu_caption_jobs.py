"""Deterministic backend/queue fixtures; no CUDA execution or performance claim."""
import copy
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from cloud_render.concat.gpu.caption_jobs import (CaptionEngine, CaptionCancelled,
    GpuSpeechUnavailable, validate_request, validate_transcript, apply_captions, subtitle)
from cloud_render.concat.gpu.worker import Handler, RenderWorker, SaladDualStackHTTPServer
from cloud_render.concat.hybrid.captions import CaptionStyle, _glyph_texture
from cloud_render.concat.hybrid.project import _caption

TIMED = {"version": 1, "cues": [{"startMs": 100, "endMs": 1900, "text": "BUT WHAT HAPPENED",
    "words": [{"text": "BUT", "startMs": 100, "endMs": 300},
              {"text": "WHAT", "startMs": 600, "endMs": 850},
              {"text": "HAPPENED", "startMs": 1100, "endMs": 1900}]}]}
REQUEST = {"version": 1, "sourceClipId": "voice", "model": "small", "language": "en", "preset": "purple-word"}
TOKEN = "x"*48


def document():
    return {"version": 1, "activeTimelineId": "timeline", "media": [], "gpuCaptionRequest": dict(REQUEST),
            "timelines": [{"id": "timeline", "tracks": [{"id": "T3", "visible": True}], "clips": [
                {"id": "voice", "trackId": "T3", "kind": "audio", "start": 3, "duration": 2}]}]}


def extract(root, doc, request, path, cancel):
    path.write_bytes(b"same decoded audio fixture")
    return 2.0, 3.0


class FakeCuda:
    identity = "mock-cuda-model-v1"
    def __init__(self): self.calls = 0
    def transcribe(self, audio, request, cancel):
        self.calls += 1
        return copy.deepcopy(TIMED)


class CaptionJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root/"project"; self.project.mkdir()
        (self.project/"concat.json").write_text(json.dumps(document()))
        self.backend = FakeCuda()
        self.engine = CaptionEngine(self.root/"cache", self.backend, extract)
    def tearDown(self): self.tmp.cleanup()

    def test_real_word_intervals_remain_half_open_and_silence_is_not_highlighted(self):
        cues = validate_transcript(TIMED, 2000)
        self.assertEqual(cues[0].active_word(100), 0)
        self.assertIsNone(cues[0].active_word(300))
        self.assertIsNone(cues[0].active_word(500))
        self.assertEqual(cues[0].active_word(1100), 2)
        self.assertIsNone(cues[0].active_word(1900))
        self.assertIn('00:00:03,100 --> 00:00:04,900', subtitle(cues, offset_ms=3000))

    def test_cache_survives_restart_and_style_change_does_not_repeat_asr(self):
        first = self.engine.run(self.project, self.root/"artifacts")
        self.assertFalse(first["result"]["cacheHit"])
        doc = json.loads((self.project/"concat.json").read_text())
        doc["gpuCaptionRequest"]["preset"] = "yellow-bar"
        (self.project/"concat.json").write_text(json.dumps(doc))
        again = CaptionEngine(self.root/"cache", self.backend, extract).run(self.project, self.root/"artifacts")
        self.assertTrue(again["result"]["cacheHit"])
        self.assertEqual(self.backend.calls, 1)
        saved = json.loads((self.project/"concat.json").read_text())
        generated = [c for c in saved["timelines"][0]["clips"] if c.get("gpuCaptionSource")]
        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0]["text"]["background"], "#ffff00")
        self.assertEqual(generated[0]["captionWords"][1]["startMs"], 500)
        self.assertEqual(generated[0]["text"]["karaokeWords"], generated[0]["captionWords"])

    def test_timed_transcript_reuse_skips_backend(self):
        doc = document(); doc["gpuCaptionRequest"]["transcript"] = TIMED
        (self.project/"concat.json").write_text(json.dumps(doc))
        result = self.engine.run(self.project, self.root/"artifacts")
        self.assertEqual(self.backend.calls, 0)
        self.assertEqual(result["result"]["timing"], "provided-word-timestamps")

    def test_invalid_intervals_and_script_only_alignment_fail_closed(self):
        for change in (lambda t: t['cues'][0]['words'][1].update(startMs=200),
                       lambda t: t['cues'][0]['words'].clear(),
                       lambda t: t['cues'][0].update(endMs=5000)):
            timed = copy.deepcopy(TIMED); change(timed)
            with self.assertRaises(ValueError): validate_transcript(timed, 2000)
        with self.assertRaises(ValueError): validate_request({**REQUEST, "model": "../../bad"})
        with self.assertRaises(ValueError): validate_request({**REQUEST, "wordTimestamps": False})

    def test_cancelled_and_gpu_unavailable_never_claim_success(self):
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(CaptionCancelled): self.engine.run(self.project, self.root/"artifacts", cancel=cancel)
        self.assertFalse((self.root/"artifacts/captions.json").exists())
        class MissingGpu(FakeCuda):
            def transcribe(self, *a): raise GpuSpeechUnavailable("No CUDA ASR runtime")
        with self.assertRaises(GpuSpeechUnavailable):
            CaptionEngine(self.root/"cache2", MissingGpu(), extract).run(self.project, self.root/"artifacts2")

    def test_purple_box_moves_without_reflow_and_requires_exact_words(self):
        doc = document(); apply_captions(doc, validate_request(REQUEST), validate_transcript(TIMED, 2000), 3.0)
        clip = doc['timelines'][0]['clips'][-1]
        cue, style = _caption(clip, 93, 147, 1, 1)
        first = _glyph_texture(cue, 0, style, 1920, 1080)
        third = _glyph_texture(cue, 2, style, 1920, 1080)
        silence = _glyph_texture(cue, None, style, 1920, 1080)
        self.assertEqual(first.size, third.size)
        self.assertNotEqual(first.tobytes(), third.tobytes())
        self.assertIn((160,32,240,255), [c for _,c in first.getcolors(first.width*first.height)])
        self.assertNotIn((160,32,240,255), [c for _,c in silence.getcolors(silence.width*silence.height)])
        clip.pop('captionWords'); clip['text'].pop('karaokeWords')
        with self.assertRaisesRegex(ValueError, 'real word timestamps'): _caption(clip, 93, 147, 1, 1)


class WorkerCaptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name)
        self.engine=CaptionEngine(root/'cache',FakeCuda(),extract)
        self.renders=[]
        def render(project, output):
            self.renders.append(json.loads((project/'concat.json').read_text()))
            output.write_bytes(b'\x00\x00\x00\x18ftypisom'+b'\0'*80)
        self.worker=RenderWorker(root/'worker', token=TOKEN, renderer=render, caption_engine=self.engine)
        with io.BytesIO() as output:
            with zipfile.ZipFile(output,'w') as z:
                z.writestr('bundle.json',json.dumps({'version':1,'project_file':'concat.json','references':[]}))
                z.writestr('project/concat.json',json.dumps(document()))
            self.data=output.getvalue()
    def tearDown(self):
        self.worker.pool.shutdown(wait=True); self.tmp.cleanup()
    def add(self):
        self.worker.accept('captionjob',io.BytesIO(self.data),len(self.data),hashlib.sha256(self.data).hexdigest(),render_mode='cuda-hybrid-v1')
        # Wait through the submitted future without closing the executor used for explicit continuation.
        import time
        for _ in range(100):
            if self.worker.status('captionjob')['state'] in ('captions_ready','failed','cancelled','waiting_for_gpu'): return
            time.sleep(.01)
        self.fail('caption fixture did not finish')

    def test_caption_only_job_retains_bundle_and_explicit_render_reuses_it(self):
        self.add(); status=self.worker.status('captionjob')
        self.assertEqual(status['state'],'captions_ready');self.assertEqual(self.renders,[])
        for ext, record in status['caption_artifacts'].items():
            path=self.worker.caption_artifact('captionjob',ext)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),record['sha256'])
        self.worker.retry('captionjob',render=True)
        self.worker.pool.shutdown(wait=True)
        self.assertEqual(self.worker.status('captionjob')['state'],'rendered')
        self.assertEqual(len(self.renders),1)
        self.assertTrue((self.worker.inputs/'captionjob/project.zip').exists())

    def test_cancel_at_caption_completion_does_not_publish_success(self):
        engine = self.worker.caption_engine
        class CompletingAfterCancel:
            def run(inner, *args, cancel=None, **kwargs):
                result = engine.run(*args, cancel=cancel, **kwargs)
                self.worker.cancel('captionjob')
                return result
        self.worker.caption_engine = CompletingAfterCancel()
        self.add()
        self.worker.pool.shutdown(wait=True)
        self.assertEqual(self.worker.status('captionjob')['state'], 'cancelled')
        self.assertEqual(self.renders, [])
        self.assertTrue((self.worker.inputs/'captionjob/project.zip').is_file())
        self.assertTrue(self.worker.caption_artifact('captionjob','json').is_file())

    def test_authenticated_artifact_endpoint(self):
        self.add()
        server=SaladDualStackHTTPServer(('::',0),Handler);server.worker=self.worker
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
            conn.request('GET','/api/jobs/captionjob/captions/json')
            response=conn.getresponse();self.assertEqual(response.status,401);response.read();conn.close()
            conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
            conn.request('GET','/api/jobs/captionjob/captions/json',headers={'Authorization':'Bearer '+TOKEN})
            response=conn.getresponse();self.assertEqual(response.status,200)
            self.assertEqual(json.loads(response.read())['sourceClipId'],'voice')
            conn.close()
        finally:
            server.shutdown();server.server_close();thread.join(5)

if __name__=='__main__': unittest.main()
