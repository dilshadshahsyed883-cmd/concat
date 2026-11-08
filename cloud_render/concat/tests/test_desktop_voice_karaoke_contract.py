"""Cross-check the opt-in word-sync path and GPU word highlight protocol."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class WordKaraokeContractTests(unittest.TestCase):
    def test_voice_transcription_requests_word_timestamps(self):
        speech = (ROOT / "upstream/src/crates/concat-speech/src/transcribe.rs").read_text()
        captions = (ROOT / "upstream/src/crates/concat/src/panes/captions.rs").read_text()
        self.assertIn("params.set_token_timestamps(true)", speech)
        self.assertIn("params.set_split_on_word(true)", speech)
        self.assertIn("word_level: self.karaoke", captions)
        self.assertIn("karaoke_words_aligned(&segments, subject.start, subject.speed)", captions)
        self.assertIn("Preserve ASR boundaries", captions)

    def test_gpu_worker_keeps_word_level_karaoke(self):
        worker = (ROOT / "cloud_render/concat/hybrid/project.py").read_text()
        self.assertIn("if karaoke:", worker)
        self.assertIn("WordTiming(", worker)


if __name__ == "__main__":
    unittest.main()
