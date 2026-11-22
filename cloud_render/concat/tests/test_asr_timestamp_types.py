"""Exercise the faster-whisper adapter without loading a model or CUDA."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from cloud_render.concat.gpu.caption_jobs import FasterWhisperCuda, _finite, validate_transcript


class AsrTimestampTypesTests(unittest.TestCase):
    def transcribe(self, start, end):
        word = SimpleNamespace(word=" hello", start=start, end=end)
        model = SimpleNamespace(transcribe=lambda *args, **kwargs:
                                (iter([SimpleNamespace(words=[word])]), None))
        backend = FasterWhisperCuda()
        backend.models["small"] = model
        with patch.object(backend, "capability", return_value={"available": True}), \
                patch.dict("sys.modules", {"faster_whisper": SimpleNamespace(WhisperModel=object)}):
            return backend.transcribe(Path("unused.wav"), {"model": "small", "language": "en"})

    def test_real_backend_timestamp_types_preserve_word_alignment(self):
        for scalar in (float, np.float32, np.float64, np.int32, np.int64):
            with self.subTest(scalar=scalar):
                transcript = self.transcribe(scalar(1), scalar(2))
                self.assertEqual(transcript["cues"][0]["words"],
                                 [{"text": "hello", "startMs": 1000, "endMs": 2000}])
                self.assertEqual(len(validate_transcript(transcript, 3000)), 1)
                json.dumps(transcript, allow_nan=False)
        transcript = self.transcribe(np.float64(.125), np.float32(.875))
        self.assertEqual(transcript["cues"][0]["startMs"], 125)
        self.assertEqual(transcript["cues"][0]["endMs"], 875)

    def test_invalid_backend_timestamps_still_fail(self):
        for value in (None, True, False, "1.0", complex(1, 0),
                      float("nan"), float("inf"), np.float32("nan"),
                      np.float64("-inf"), np.bool_(True), np.array([1.0])):
            for field in ("start", "end"):
                with self.subTest(value=repr(value), field=field):
                    from cloud_render.concat.gpu.asr_alignment import (
                        AsrWordAlignmentError, segments_to_cues
                    )
                    word = SimpleNamespace(word="hello",
                        start=value if field == "start" else 0,
                        end=value if field == "end" else 2)
                    # The strict parser rejects malformed model scalars,
                    # even though GPU ASR can recover from valid segment bounds.
                    with self.assertRaisesRegex(AsrWordAlignmentError, "Invalid ASR word " + field):
                        segments_to_cues([SimpleNamespace(words=[word])], model="small")
                    # Without real segment bounds, no timing can be estimated.
                    # The alternate ASR pass must also fail safely.
                    with self.assertRaisesRegex(ValueError, "after 2 passes"):
                        self.transcribe(value if field == "start" else 0,
                                        value if field == "end" else 2)

    def test_project_numeric_validation_remains_strict(self):
        for value in (np.float64(1), np.float32(1), "1", True):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    _finite(value, "caption source duration")


if __name__ == "__main__":
    unittest.main()
