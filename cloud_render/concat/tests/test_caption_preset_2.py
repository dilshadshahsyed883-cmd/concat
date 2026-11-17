"""Rasterize caption glyph fixtures only; never invoke a video encoder."""
from pathlib import Path
import tomllib
import unittest
from cloud_render.concat.hybrid.captions import _glyph_texture
from cloud_render.concat.hybrid.fonts import resolve_font
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
        for family,weight in [('Hanken Grotesk',600),('Hanken Grotesk',700),('Inter',900),('Inter',700),
                              ('Montserrat',800),('Archivo Black',400),('Anton',400)]:
            self.assertTrue(resolve_font(family,weight).is_file())
        with self.assertRaises(ValueError): resolve_font('../../etc/passwd',600)
        with self.assertRaises(ValueError): resolve_font('Inter',333)

if __name__ == '__main__': unittest.main()
