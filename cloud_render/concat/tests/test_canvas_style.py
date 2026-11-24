import unittest
import numpy as np
from cloud_render.concat.hybrid.canvas_style import MODES, parse_style, palette, style_pixels

class CanvasStyleTests(unittest.TestCase):
    def test_all_styles_have_unique_outputs_and_controls(self):
        source=np.ones((16,32,4),dtype=np.float32)
        source[:,:,0]=np.arange(32)/32
        source[:,:,1]=np.arange(16)[:,None]/16
        source[:,:,2]=90/255
        colors=palette(source,np)
        outputs=[]
        for mode in MODES:
            style=parse_style(dict(version=1,mode=mode,color="#18334f",softness=.6,controls={}))
            pixels=style_pixels(style,64,36,source,colors,np)
            self.assertEqual(pixels.shape,(36,64,3))
            self.assertTrue(np.isfinite(pixels).all())
            self.assertTrue(((pixels>=0)&(pixels<=1)).all())
            self.assertFalse(any(np.array_equal(pixels,p) for p in outputs),mode)
            outputs.append(pixels)
            off=parse_style(dict(version=1,mode=mode,color="#18334f",softness=.6,controls={"opacity":0}))
            np.testing.assert_allclose(style_pixels(off,64,36,source,colors,np),np.broadcast_to(style.color,(36,64,3)))
    def test_invalid_style_fails_closed(self):
        for update in ({"version":True},{"mode":"future"},{"color":"#12 456"},{"softness":float("nan")},{"controls":{"density":2}}):
            value=dict(version=1,mode=MODES[0],color="#123456",softness=.5,controls={})
            value.update(update)
            with self.assertRaises(ValueError):
                parse_style(value)
    def test_palette_is_stable_and_dominant(self):
        source=np.ones((16,16,4),dtype=np.float32)
        source[:,:,:3]=[.8,.1,.2]
        source[:4,:,:3]=[.1,.3,.9]
        a,b=palette(source,np)
        np.testing.assert_allclose(a,[.8,.1,.2])
        np.testing.assert_allclose(b,[.1,.3,.9])
        np.testing.assert_allclose(palette(source,np),[a,b])

if __name__=="__main__":
    unittest.main()
