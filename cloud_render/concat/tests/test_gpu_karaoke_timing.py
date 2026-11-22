"""Segment-based karaoke fallback; no GPU, audio download, or container build."""
import copy
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace as Item
import unittest
import zipfile

from cloud_render.concat.gpu.asr_alignment import AsrWordAlignmentError, segments_to_cues
from cloud_render.concat.gpu.caption_jobs import CaptionEngine, validate_transcript
from cloud_render.concat.gpu.worker import RenderWorker
from cloud_render.concat.tests import test_gpu_asr_alignment as alignment_fixtures
from cloud_render.concat.tests.test_gpu_asr_alignment import segment, word


def timed_segment(*words, start=.1, end=1.1, text="hello world"):
    result = segment(*words, text=text)
    result.start, result.end = start, end
    return result


def tolerant(*segments):
    return segments_to_cues(segments, model="small", allow_estimated=True)


class SegmentKaraokeTests(unittest.TestCase):
    def test_bad_word_intervals_keep_full_segment_text_and_boundaries(self):
        for start, end in ((.4, .4), (.5, .3), (None, .4),
                           (math.nan, math.inf), (.2001, .2004), (.2, .6)):
            with self.subTest(start=start, end=end):
                result = tolerant(timed_segment(word("hello", .1, .3),
                                                word("world", start, end)))
                cues = validate_transcript(result, 2000)
                self.assertEqual(cues[0].text, "hello world")
                self.assertEqual((cues[0].start_ms, cues[-1].end_ms), (100, 1100))
                self.assertEqual([w.text for w in cues[0].words], ["hello", "world"])
                self.assertEqual(cues[0].active_word(100), 0)
                self.assertEqual(cues[0].active_word(600), 1)
                self.assertIsNone(cues[0].active_word(1100))
                self.assertEqual(result["alignment"],
                                 {"estimatedSegments": 1, "estimatedWords": 2})

    def test_valid_words_and_silence_gaps_remain_exact(self):
        result = tolerant(
            timed_segment(word("hello", .1, .3), word("world", .6, .8)),
            timed_segment(word("bad", 1.5, 1.5), start=1.2, end=2, text="bad timing"),
            timed_segment(word("end", 2.5, 2.8), start=2.4, end=3, text="end"),
        )
        cues = validate_transcript(result, 3000)
        self.assertEqual([(w.start_ms, w.end_ms) for w in cues[0].words],
                         [(100, 300), (600, 800)])
        self.assertIsNone(cues[0].active_word(500))
        self.assertEqual((cues[-1].words[0].start_ms, cues[-1].words[0].end_ms),
                         (2500, 2800))
        self.assertEqual(result["alignment"]["estimatedSegments"], 1)

    def test_segment_182_word_6_does_not_abort_the_transcript(self):
        segments = [timed_segment(word("ok", i+.1, i+.9),
                                  start=i, end=i+1, text="ok") for i in range(181)]
        words = [word("word", 181+i/10, 181+(i+1)/10) for i in range(5)]
        words.append(word("last", 181.5, 181.5))
        segments.append(timed_segment(*words, start=181, end=182,
                                      text="word word word word word last"))
        result = tolerant(*segments)
        cues = validate_transcript(result, 182000)
        self.assertEqual(len(cues), 182)
        self.assertEqual(cues[-1].text, "word word word word word last")
        self.assertEqual((cues[-1].start_ms, cues[-1].end_ms), (181000, 182000))

    def test_missing_or_incomplete_word_list_uses_complete_segment_text(self):
        for words in ([], [word("hello", .1, .3)], [word("hello world", .1, .9)]):
            result = tolerant(timed_segment(*words, text="hello world!"))
            cues = validate_transcript(result, 2000)
            self.assertEqual(cues[0].text, "hello world!")
            self.assertEqual([w.text for w in cues[0].words], ["hello", "world!"])

    def test_estimates_group_long_lines_without_dropping_words(self):
        tokens = ["word"+str(i) for i in range(24)]
        result = tolerant(timed_segment(start=1, end=4, text=" ".join(tokens)))
        cues = validate_transcript(result, 4000)
        self.assertEqual([w.text for c in cues for w in c.words], tokens)
        self.assertEqual((cues[0].start_ms, cues[-1].end_ms), (1000, 4000))
        self.assertTrue(all(len(c.words) <= 7 for c in cues))
        self.assertEqual(result["alignment"]["estimatedWords"], 24)

    def test_invalid_segment_timing_still_fails(self):
        for start, end in ((None, 1), (-1, 1), (math.nan, 1),
                           (0, math.inf), (1, 1), (2, 1), (0, .001)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(AsrWordAlignmentError):
                    tolerant(timed_segment(start=start, end=end))
        with self.assertRaises(AsrWordAlignmentError):
            tolerant(timed_segment(word("hello", .1, .3), word("world", .5, 1)),
                     timed_segment(start=.9, end=2))
        with self.assertRaises(ValueError):
            validate_transcript(tolerant(timed_segment(text="")), 2000)

    def test_words_outside_the_segment_use_bounded_estimates(self):
        result = tolerant(timed_segment(word("hello", 0, .3), word("world", .6, 1.3)))
        cues = validate_transcript(result, 1100)
        self.assertEqual((cues[0].start_ms, cues[-1].end_ms), (100, 1100))

    def test_valid_segment_keeps_model_timing_label(self):
        result = tolerant(timed_segment(word("hello", .1, .3), word("world", .6, .8)))
        self.assertEqual(result["timing"], "model-word-timestamps")
        self.assertEqual(result["alignment"]["estimatedWords"], 0)

    def test_cancellation_is_not_swallowed_by_estimation(self):
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("cancelled")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            segments_to_cues([timed_segment()], model="small",
                             allow_estimated=True, check_cancel=cancel)

    def test_cuda_backend_accepts_segment_estimation_in_one_pass(self):
        harness = alignment_fixtures.FallbackAlignmentTests()
        calls, result = harness.call_backend([
            [timed_segment(word("hello", .1, .3), word("world", .4, .4))]
        ])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["timing"], "segment-estimated-word-timestamps")
        self.assertEqual(validate_transcript(result, 2000)[0].text, "hello world")


class EstimatedCaptionCacheTests(unittest.TestCase):
    def test_bad_word_timing_can_finish_the_worker_render(self):
        transcript = tolerant(timed_segment(word("hello", .1, .3), word("world", .4, .4)))
        class Backend:
            identity = "mock-estimated-cuda"
            def transcribe(self, *args):
                return copy.deepcopy(transcript)
        def extract(root, document, request, audio, cancel):
            audio.write_bytes(b"mocked audio")
            return 2.0, 0.0
        document = {"version": 1, "activeTimelineId": "t", "media": [],
                    "gpuCaptionRequest": {"version": 1, "sourceClipId": "voice",
                        "model": "small", "preset": "purple-word", "renderAfter": True},
                    "timelines": [{"id": "t", "tracks": [{"id": "audio"}],
                        "clips": [{"id": "voice", "trackId": "audio", "kind": "audio",
                                   "start": 0, "duration": 2}]}]}
        renders = []
        def render(project, output):
            renders.append(json.loads((project/"concat.json").read_text()))
            output.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00"*80)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = CaptionEngine(root/"cache", Backend(), extract)
            worker = RenderWorker(root/"worker", token="x"*48,
                                  renderer=render, caption_engine=engine)
            try:
                with io.BytesIO() as stream:
                    with zipfile.ZipFile(stream, "w") as archive:
                        archive.writestr("bundle.json", json.dumps(
                            {"version": 1, "project_file": "concat.json", "references": []}))
                        archive.writestr("project/concat.json", json.dumps(document))
                    bundle = stream.getvalue()
                worker.accept("estimated", io.BytesIO(bundle), len(bundle),
                              hashlib.sha256(bundle).hexdigest(), render_mode="cuda-hybrid-v1")
                worker.pool.shutdown(wait=True)
                saved = worker.status("estimated")
                self.assertEqual(saved["state"], "rendered", saved)
                self.assertEqual(saved["caption_estimated_words"], 2)
                self.assertEqual(len(renders), 1)
                words = renders[0]["timelines"][0]["clips"][-1]["captionWords"]
                self.assertEqual([w["text"] for w in words], ["HELLO", "WORLD"])
            finally:
                worker.pool.shutdown(wait=True, cancel_futures=True)

    def test_estimated_karaoke_survives_cache_artifacts_and_project_application(self):
        transcript = tolerant(timed_segment(word("hello", .1, .3),
                                            word("world", .4, .4)))
        class Backend:
            identity = "mock-estimated-cuda"
            calls = 0
            def transcribe(self, *args):
                self.calls += 1
                return copy.deepcopy(transcript)
        def extract(root, document, request, audio, cancel):
            audio.write_bytes(b"stable mocked audio")
            return 2.0, 3.0
        document = {"version": 1, "activeTimelineId": "t", "media": [],
                    "gpuCaptionRequest": {"version": 1, "sourceClipId": "voice",
                        "model": "small", "language": "en", "preset": "purple-word",
                        "renderAfter": True},
                    "timelines": [{"id": "t", "tracks": [{"id": "audio"}],
                        "clips": [{"id": "voice", "trackId": "audio", "kind": "audio",
                                   "start": 3, "duration": 2}]}]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root/"project"
            project.mkdir()
            path = project/"concat.json"
            path.write_text(json.dumps(document))
            backend = Backend()
            first = CaptionEngine(root/"cache", backend, extract).run(project, root/"first")
            status = []
            again = CaptionEngine(root/"cache", backend, extract).run(
                project, root/"again", status=lambda **fields: status.append(fields))
            self.assertFalse(first["result"]["cacheHit"])
            self.assertTrue(again["result"]["cacheHit"])
            self.assertEqual(backend.calls, 1)
            for result in (first["result"], again["result"]):
                self.assertEqual(result["timing"], "segment-estimated-word-timestamps")
                self.assertEqual(result["alignment"],
                                 {"estimatedSegments": 1, "estimatedWords": 2})
            artifact = json.loads((root/"again/captions.json").read_text())
            self.assertEqual(artifact["alignment"], transcript["alignment"])
            self.assertIn("00:00:03,100 --> 00:00:04,100",
                          (root/"again/captions.srt").read_text())
            clip = json.loads(path.read_text())["timelines"][0]["clips"][-1]
            self.assertEqual(clip["text"]["content"], "HELLO WORLD")
            self.assertEqual([w["text"] for w in clip["captionWords"]], ["HELLO", "WORLD"])
            self.assertEqual(clip["captionWords"], clip["text"]["karaokeWords"])
            self.assertEqual(status[-1]["caption_estimated_words"], 2)


if __name__ == "__main__":
    unittest.main()
