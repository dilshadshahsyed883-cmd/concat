"""Fixed Canvas openings and parent/content transforms. Pixel clipping stays on CUDA."""
from dataclasses import dataclass, replace
from math import cos, sin, radians
from .canvas_shapes import number, rounded_distance

@dataclass(frozen=True)
class Viewport:
    scale: float
    x: float
    y: float
    width: float
    height: float
    rotation: float
    radius: float

    def compose(self, content, aspect=1920 / 1080):
        a = radians(self.rotation)
        c, s = cos(a), sin(a)
        return replace(content, scale=self.scale * content.scale,
            pan_x=self.x + self.scale * (c * content.pan_x - s * content.pan_y / aspect),
            pan_y=self.y + self.scale * (s * content.pan_x * aspect + c * content.pan_y),
            rotation_degrees=self.rotation + content.rotation_degrees)

    def coverage(self, u, v, width=1920, height=1080):
        """Numeric reference for tests; never used to process image/video pixels."""
        short = min(width, height)
        dx, dy = (u - .5 - self.x) * width / short, (v - .5 - self.y) * height / short
        a = radians(self.rotation)
        x, y = dx * cos(a) + dy * sin(a), -dx * sin(a) + dy * cos(a)
        half_w, half_h = self.width * width / short / 2, self.height * height / short / 2
        d = rounded_distance(x, y, half_w, half_h, self.radius * height / short)
        band = 1 / short
        t = min(max((d + band * .5) / band, 0), 1)
        return 1 - t * t * (3 - 2 * t)


def parse_viewport(spec):
    if not isinstance(spec, dict) or set(spec) != {'version','scale','x','y','width','height','rotation','radius'} or type(spec['version']) is not int or spec['version'] != 1:
        raise ValueError('Unsupported Canvas viewport metadata')
    return Viewport(number(spec['scale'],'viewport scale',.0001,8),
        number(spec['x'],'viewport x',-1,1), number(spec['y'],'viewport y',-1,1),
        number(spec['width'],'viewport width',.0001,1), number(spec['height'],'viewport height',.0001,1),
        number(spec['rotation'],'viewport rotation',-180,180), number(spec['radius'],'viewport radius',0,.5))


def clip_viewport(picture, viewport):
    """Apply output-space aperture after content placement, fully on the GPU."""
    from .contract import cuda, rgba
    cp = cuda()
    pixels = rgba(picture)
    height, width = pixels.shape[:2]
    short = min(width,height)
    yy, xx = cp.ogrid[:height,:width]
    dx = ((xx + .5) / width - .5 - viewport.x) * width / short
    dy = ((yy + .5) / height - .5 - viewport.y) * height / short
    a = radians(viewport.rotation)
    x, y = dx * cos(a) + dy * sin(a), -dx * sin(a) + dy * cos(a)
    half_w, half_h = viewport.width * width / short / 2, viewport.height * height / short / 2
    radius = min(viewport.radius * height / short,half_w,half_h)
    qx, qy = cp.abs(x) - half_w + radius, cp.abs(y) - half_h + radius
    distance = cp.sqrt(cp.maximum(qx,0)**2 + cp.maximum(qy,0)**2) + cp.minimum(cp.maximum(qx,qy),0) - radius
    band = 1 / short
    t = cp.clip((distance + band * .5) / band,0,1)
    result = pixels.copy()
    result[...,3] *= (1-t*t*(3-2*t)).astype(cp.float32)
    return result
