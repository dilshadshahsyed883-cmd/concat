"""Advanced Canvas background parsing and actual procedural pixel contracts."""
import unittest
import numpy as np
from cloud_render.concat.hybrid.canvas import parse_background, fitted_rect
from cloud_render.concat.hybrid.procedural_canvas import STYLES, render_pattern


def spec(style, **overrides):
    values={"style":style,"color":"#101c2d","accent":"#aabeda",
            "intensity":.9,"density":.5,"softness":.5,"opacity":1.,"spread":.65,"size":.5}
    values.update(overrides)
    return {"kind":"procedural","pattern":values}


class ProceduralCanvasTests(unittest.TestCase):
    def test_each_style_is_distinct_and_strength_zero_is_base(self):
        source=np.ones((36,64,4),dtype=np.float32)
        index=np.arange(36*64).reshape(36,64)
        source[...,0]=(index%256)/255
        source[...,1]=((index//64)*7)/255
        source[...,2]=150/255
        pictures=[]
        for style in STYLES:
            with self.subTest(style=style):
                background=parse_background(spec(style))
                frame=render_pattern(np,source,(64,36),background,fitted_rect)
                self.assertEqual(frame.shape,(36,64,4))
                self.assertTrue(np.isfinite(frame).all())
                self.assertTrue(((frame>=0)&(frame<=1)).all())
                self.assertFalse(any(np.array_equal(frame,prior) for prior in pictures))
                pictures.append(frame)
                base=render_pattern(np,source,(64,36),parse_background(spec(style,intensity=0)),fitted_rect)
                np.testing.assert_allclose(base[...,:3],np.broadcast_to(np.array([16,28,45])/255,base[...,:3].shape),atol=1e-6)
    def test_source_patterns_react_to_source_and_static_ones_do_not(self):
        a=np.ones((36,64,4),np.float32);b=a.copy();b[...,:3]=.1
        for style in STYLES:
            bg=parse_background(spec(style))
            aa=render_pattern(np,a,(64,36),bg,fitted_rect)
            bb=render_pattern(np,b,(64,36),bg,fitted_rect)
            self.assertEqual(not np.array_equal(aa,bb),bg.uses_source,style)
    def test_invalid_patterns_fail_closed(self):
        for invalid in [spec("unknown"),spec(STYLES[0],density=2),spec(STYLES[0],size=True),spec(STYLES[0],color="red")]:
            with self.assertRaises(ValueError):parse_background(invalid)
