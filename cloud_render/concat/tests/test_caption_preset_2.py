"""Rasterize caption glyph fixtures only; never invoke a video encoder."""
from pathlib import Path
import tomllib
import unittest
import re
from pathlib import Path
from cloud_render.concat.hybrid.captions import _glyph_texture
from cloud_render.concat.hybrid.fonts import FACES, resolve_font
from cloud_render.concat.tests.test_gpu_compat_validation import minimal_project, native_caption, load_fixture

ROOT = Path(__file__).resolve().parents[3]

class YellowCaptionTests(unittest.TestCase):
    def test_preset_colors_fonts_and_independent_editing(self):
        preset = tomllib.loads((ROOT/'upstream/src/crates/concat/text-presets/cuda-preset-2-yellow-bar/preset.toml').read_text())
        document = minimal_project()
        native_caption(document, **preset['style'])
        cue, style = load_fixture(document).captions[0]
        self.assertFalse(cue.words)
        self.assertEqual(style.fill, (0,0,0,255))
        self.assertEqual(style.background, (255,255,0,255))
        yellow = _glyph_texture(cue, None, style, 1920, 1080)
        self.assertIn((255,255,0,255), [color for _,color in yellow.getcolors(yellow.width*yellow.height)])
        document = minimal_project()
        native_caption(document, content='EDITABLE FONT AND COLORS', fontFamily='Montserrat', fontWeight=800,
                       color='#ffffff', background='#ff4400', backgroundRadius=.02)
        _, changed = load_fixture(document).captions[-1]
        self.assertEqual(changed.fill, (255,255,255,255))
        self.assertEqual(changed.background, (255,68,0,255))
        self.assertEqual(style.background, (255,255,0,255))

    def test_all_exposed_faces_exist_and_unknown_faces_fail(self):
        for family, weights in FACES.items():
            for weight in weights:
                self.assertTrue(resolve_font(family, weight).is_file())
        self.assertEqual(resolve_font("Archivo Black", 900), resolve_font("Archivo Black", 400))
        with self.assertRaises(ValueError): resolve_font('../../etc/passwd',600)
        with self.assertRaises(ValueError): resolve_font('Inter',333)

    def test_desktop_caption_font_choices_match_worker_faces(self):
        root = Path(__file__).resolve().parents[3]
        desktop = root / "upstream/src/crates/concat"
        preview = (desktop / "src/studio/caption_preview.rs").read_text(encoding="utf-8")
        setter = preview.split("let (family, weight) = match index {", 1)[1].split("};", 1)[0]
        choices = re.findall(r'\d+ => \("([^"]+)", (\d+)\.0\)', setter)
        expected = {(family, weight) for family, weights in FACES.items() for weight in weights}
        self.assertEqual({(family, int(weight)) for family, weight in choices}, expected)
        self.assertEqual(len(choices), len(expected))
        for relative in ("ui/workspace/caption-preset-preview.slint", "ui/inspector/text-panel.slint"):
            ui = (desktop / relative).read_text(encoding="utf-8")
            options = re.search(r'options: \[("Hanken Grotesk SemiBold".*?)\];', ui).group(1)
            self.assertEqual(len(re.findall(r'"[^"]+"', options)), len(choices))
        inspector = (desktop / "ui/inspector/text-panel.slint").read_text(encoding="utf-8")
        families = re.findall(r'index == (\d+) \? "([^"]+)"', inspector.split("root.set-text(ClipTextField.font-family,", 1)[1].split(";", 1)[0])
        weights = re.findall(r'index == (\d+) \? (\d+)', inspector.split("root.set(ClipField.font-weight,", 1)[1].split(";", 1)[0])
        self.assertEqual(dict(families), {str(i): family for i, (family, _) in enumerate(choices)})
        self.assertEqual(dict(weights), {str(i): weight for i, (_, weight) in enumerate(choices)})

if __name__ == '__main__': unittest.main()
