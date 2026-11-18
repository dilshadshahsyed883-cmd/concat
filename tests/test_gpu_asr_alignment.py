"""Regression tests for GPU caption Issue #83; no GPU or model downloads."""
from __future__ import annotations

import copy
import math
import sys
import types
import unittest
from unittest.mock import patch

from cloud_render.concat.gpu.asr_alignment import (
    AsrWordAlignmentError, segments_to_cues
)
from cloud_render.concat.gpu.caption_jobs import (
    FasterWhisperCuda, validate_transcript, subtitle
)


def word(text, start, end):
    return types.SimpleNamespace(word=text, start=start, end=end)


def segment(*words, text=None):
    return types.SimpleNamespace(
        words=list(words), text=text if text is not None
        else " ".join(w.word for w in words)
    )


def make(*segments):
    return segments_to_cues(segments, model="small")


class AsrWordAlignmentTests(unittest.TestCase):
    def test_real_word_boundaries_and_silence_gap_remain_exact(self):
        result = make(segment(word("Hello", .1, .3), word("world", .6, .85)))
        cues = validate_transcript(result, 2000)
        self.assertEqual([(w.start_ms, w.end_ms) for w in cues[0].words],
                         [(100, 300), (600, 850)])
        self.assertIsNone(cues[0].active_word(400))
        self.assertIn("00:00:03,100 --> 00:00:03,850",
                      subtitle(cues, offset_ms=3000))
        self.assertIn("00:00:03.100 --> 00:00:03.850",
                      subtitle(cues, vtt=True, offset_ms=3000))

    def test_scalar_float_conversion_preserves_real_timing(self):
        class FloatScalar:
            def __float__(self):
                return .125
        result = make(segment(word("hello", FloatScalar(), .275)))
        cues = validate_transcript(result, 300)
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (125, 275))
        try:
            import numpy
        except ImportError:
            return
        result = make(segment(word("hello", numpy.float32(.25), numpy.float32(.5))))
        self.assertEqual(result["cues"][0]["words"][0]["startMs"], 250)

    def test_unaligned_punctuation_attaches_to_timed_word_without_synthesized_time(self):
        result = make(segment(
            word("hello", .1, .3),
            word(",", None, None),
            word("world", .5, .8),
            word("!", float("nan"), float("inf")),
        ))
        cues = validate_transcript(result, 1000)
        self.assertEqual([w.text for w in cues[0].words], ["hello,", "world!"])
        self.assertEqual([(w.start_ms, w.end_ms) for w in cues[0].words],
                         [(100, 300), (500, 800)])
        self.assertIsNone(cues[0].active_word(350))
        self.assertEqual(cues[0].text, "hello, world!")

    def test_unaligned_leading_punctuation_attaches_to_first_real_word(self):
        result = make(segment(word("(", None, None), word("hello", .1, .3)))
        self.assertEqual(result["cues"][0]["text"], "(hello")
        self.assertEqual(result["cues"][0]["words"][0]["startMs"], 100)

    def test_missing_or_nonfinite_substantive_words_fail_closed(self):
        bad_values = (None, float("nan"), float("inf"), -float("inf"),
                      -.1, True, "0.2", object(), complex(1, 2))
        for value in bad_values:
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(AsrWordAlignmentError, "Invalid ASR word start"):
                    make(segment(word("secret", value, .6)))

    def test_invalid_end_zero_interval_and_overlaps_fail(self):
        records = [
            segment(word("hello", .1, None)),
            segment(word("hello", .1, float("nan"))),
            segment(word("hello", .3, .3)),
            segment(word("hello", .3, .2)),
        ]
        for record in records:
            with self.subTest(record=repr(record)):
                with self.assertRaises(AsrWordAlignmentError):
                    make(record)
        with self.assertRaisesRegex(AsrWordAlignmentError, "Overlapping"):
            make(segment(word("hello", .1, .4), word("world", .35, .6)))
        with self.assertRaisesRegex(AsrWordAlignmentError, "Overlapping"):
            make(segment(word("hello", .1, .4)),
                 segment(word("world", .35, .6)))

    def test_substantive_word_without_alignment_not_silently_dropped(self):
        with self.assertRaisesRegex(AsrWordAlignmentError, "Missing ASR word timestamps"):
            make(segment(text="secret spoken words"))
        with self.assertRaises(AsrWordAlignmentError):
            make(segment(word(".", None, None), text="."))
        with self.assertRaises(AsrWordAlignmentError) as context:
            make(segment(word("secret", None, .6)))
        self.assertNotIn("secret", str(context.exception))
        self.assertIn("segment=1, word=1", str(context.exception))

    def test_check_cancel_applies_during_iteration(self):
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            segments_to_cues([segment(word("hello", .1, .3))], model="small",
                             check_cancel=lambda: (_ for _ in ()).throw(
                                 RuntimeError("cancelled")))


class FallbackAlignmentTests(unittest.TestCase):
    def call_backend(self, responses):
        backend = FasterWhisperCuda()
        calls = []
        class MockModel:
            def transcribe(self, path, *, language, **kwargs):
                calls.append((path, language, kwargs))
                return iter(responses[len(calls) - 1]), None
        backend.models["small"] = MockModel()
        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = object
        request = {"model": "small", "language": "en"}
        with patch.dict(sys.modules, {"faster_whisper": fake}), patch.object(
            FasterWhisperCuda, "capability",
            return_value={"available": True, "backend": "faster-whisper",
                          "device": "cuda"}
        ):
            try:
                output = backend.transcribe("narration.wav", request)
            except Exception as exc:
                return calls, exc
        return calls, output

    def test_normal_valid_audio_stays_one_pass_with_vad(self):
        calls, result = self.call_backend([
            [segment(word("hello", .1, .4))],
        ])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][2]["vad_filter"])
        self.assertNotIn("condition_on_previous_text", calls[0][2])
        self.assertEqual(validate_transcript(result, 1000)[0].text, "hello")

    def test_bad_alignment_retranscribes_once_with_alternate_settings(self):
        calls, result = self.call_backend([
            [segment(word("hello", None, .4))],
            [segment(word("hello", .15, .4))],
        ])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][2]["vad_filter"])
        self.assertFalse(calls[1][2]["vad_filter"])
        self.assertFalse(calls[1][2]["condition_on_previous_text"])
        cues = validate_transcript(result, 1000)
        self.assertEqual(cues[0].words[0].start_ms, 150)

    def test_two_bad_alignments_fail_with_safe_context_not_fake_captions(self):
        calls, result = self.call_backend([
            [segment(word("private speech", None, .4))],
            [segment(word("private speech", float("nan"), .4))],
        ])
        self.assertEqual(len(calls), 2)
        self.assertIsInstance(result, ValueError)
        self.assertIn("after 2 passes", str(result))
        self.assertIn("model=small", str(result))
        self.assertNotIn("private speech", str(result))

    def test_empty_unaligned_segment_triggers_second_pass(self):
        calls, result = self.call_backend([
            [segment(text="Hello")],
            [segment(word("Hello", .1, .4))],
        ])
        self.assertEqual(len(calls), 2)
        self.assertEqual(validate_transcript(result, 1000)[0].text, "Hello")


if __name__ == "__main__":
    unittest.main()
