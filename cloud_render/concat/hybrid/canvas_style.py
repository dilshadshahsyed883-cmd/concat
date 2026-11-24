"""Versioned Canvas background styles. Production synthesis stays on CUDA."""
from dataclasses import dataclass
from math import isfinite
from .contract import cuda, rgba

MODES = ("ambientClouds", "frostedGlass", "radialSpotlight", "halftoneArena",
         "diagonalStreaks", "monochrome", "topographicWaves", "carbonFiber",
         "prismRefraction", "cinematicBokeh", "texturedPaper", "lightTunnel", "adaptiveColors")

@dataclass(frozen=True)
class StyledBackground:
    mode: str
    color: tuple
    softness: float
    intensity: float = .75
    density: float = .5
    opacity: float = 1.
    spread: float = .5
    pattern_size: float = .5

    @property
    def kind(self):
        return "styled"

    @property
    def dynamic(self):
        return self.mode in ("frostedGlass", "monochrome", "prismRefraction")


def parse_style(value):
    if not isinstance(value, dict) or set(value) != {"version", "mode", "color", "softness", "controls"}:
        raise ValueError("Invalid Canvas backdrop metadata")
    if type(value["version"]) is not int or value["version"] != 1 or value["mode"] not in MODES:
        raise ValueError("Unsupported Canvas backdrop style/version")
    color = value["color"]
    if not isinstance(color, str) or len(color) not in (7, 9) or color[0] != "#" or any(c not in "0123456789abcdefABCDEF" for c in color[1:]):
        raise ValueError("Invalid Canvas backdrop color")
    controls = value["controls"]
    defaults = {"intensity": .75, "density": .5, "opacity": 1., "spread": .5, "patternSize": .5}
    if not isinstance(controls, dict) or not set(controls) <= set(defaults):
        raise ValueError("Invalid Canvas backdrop controls")
    controls = {**defaults, **controls}
    if any(type(x) not in (int, float) or not isfinite(x) or not 0 <= x <= 1
           for x in [value["softness"], *controls.values()]):
        raise ValueError("Canvas backdrop controls must be finite and normalized")
    return StyledBackground(value["mode"], tuple(int(color[i:i+2], 16)/255. for i in (1, 3, 5)),
                            float(value["softness"]), *(float(controls[k]) for k in defaults))


def _sample(source, u, v, xp):
    h, w = source.shape[:2]
    xx = xp.floor(xp.clip(u, 0, 1)*(w-1)+.5).astype(xp.int32)
    yy = xp.floor(xp.clip(v, 0, 1)*(h-1)+.5).astype(xp.int32)
    p = source[yy, xx]
    return p[..., :3]*p[..., 3:4]


def palette(source, xp):
    """Same 32x32 deterministic 4x4x4 histogram as the native renderer."""
    grid = (xp.arange(32, dtype=xp.float64)+.5)/32
    u, v = xp.meshgrid(grid, grid)
    colors = _sample(source, u, v, xp).reshape(-1, 3).astype(xp.float64)
    q = xp.minimum(xp.floor(colors*4), 3).astype(xp.int32)
    bins = q[:, 0]*16+q[:, 1]*4+q[:, 2]
    counts = xp.bincount(bins, minlength=64)
    order = xp.argsort(-counts*64+xp.arange(64))
    first, second = order[0], xp.where(counts[order[1]]>0, order[1], order[0])
    sums = xp.stack([xp.bincount(bins, weights=colors[:, j], minlength=64) for j in range(3)], axis=1)
    return sums[first]/xp.maximum(counts[first], 1), sums[second]/xp.maximum(counts[second], 1)


def style_pixels(style, width, height, source, colors, xp):
    """Vectorized mathematical reference; caller explicitly selects CUDA or test NumPy."""
    u, v = xp.meshgrid((xp.arange(width,dtype=xp.float64)+.5)/width,
                       (xp.arange(height,dtype=xp.float64)+.5)/height)
    aspect = width/height
    x, y = (u-.5)*aspect, v-.5
    n = (5+25*style.density)/(.4+1.2*style.pattern_size)
    base = xp.array(style.color)
    def mix(a, b, t):
        a, b = xp.asarray(a), xp.asarray(b)
        t = xp.clip(t, 0, 1)
        if t.ndim == 2:
            t = t[..., None]
        return a*(1-t)+b*t
    def frac(a):
        return a-xp.floor(a)
    def smooth(a,b,t):
        t=xp.clip((t-a)/(b-a),0,1)
        return t*t*(3-2*t)
    def gauss(cx,cy,r):
        return xp.exp(-((x-cx)**2+(y-cy)**2)/(r*r))
    bright, dark = mix(base, (1,1,1), .7), mix(base, (0,0,0), .75)
    def src(su, sv):
        if source is None:
            return xp.broadcast_to(base, (height,width,3))
        a=source.shape[1]/source.shape[0]
        return _sample(source,.5+(su-.5)*min(aspect/a,1),.5+(sv-.5)*min(a/aspect,1),xp)
    m=style.mode
    if m=="ambientClouds":
        r=.3+.7*style.spread
        result=mix(mix(dark,bright,gauss(-.35,-.15,r)*.8),(.38,.24,.68),gauss(.4,.25,r)*.6)
    elif m=="frostedGlass":
        d=.012+.075*style.softness
        total=xp.zeros((height,width,3),dtype=xp.float64)
        for j in range(-2,3):
            for i in range(-2,3):
                total+=src(u+i*d/aspect,v+j*d)/25
        t=frac(u*3)
        result=mix(total,bright,xp.where((t>=.04)&(t<.96),.28,.58))
    elif m=="radialSpotlight":
        result=mix(dark,bright,gauss(0,-.08,.18+.85*style.spread))
    elif m=="halftoneArena":
        dx,dy=frac(x*n)-.5,frac(y*n)-.5
        fade=1-v
        result=mix(dark,bright,(1-smooth(.11+fade*.17,.15+fade*.17,xp.hypot(dx,dy)))*.9)
    elif m=="diagonalStreaks":
        band=1-smooth(.025,.08+.3*style.spread,xp.abs(frac((x+y*.75)*(n*.25))-.5))
        result=mix(dark,bright,band*(.35+.65*(1-v)))
    elif m=="monochrome":
        p=src(u,v)
        lum=(p[...,0]*.2126+p[...,1]*.7152+p[...,2]*.0722)*.6
        result=xp.repeat(lum[...,None],3,axis=2)
    elif m=="topographicWaves":
        z=y+.11*xp.sin(x*5)+.06*xp.sin(x*11+y*6)
        result=mix(dark,bright,1-smooth(.035,.095,xp.abs(frac(z*n*.55)-.5)))
    elif m=="carbonFiber":
        f=xp.where((xp.floor(x*n)+xp.floor(y*n))%2<1,frac(x*n),frac(y*n))
        result=mix(dark,bright,.1+.22*xp.sin(xp.pi*f)**2)
    elif m=="prismRefraction":
        shift=xp.sin(xp.floor(u*n*.4)*1.7)*.075
        p=xp.stack((src(u+shift-.012,v)[...,0],src(u+shift,v)[...,1],src(u+shift+.012,v)[...,2]),axis=2)
        result=mix(p,bright,.12+.13*frac(u*n*.4))
    elif m=="cinematicBokeh":
        result=dark
        for i in range(12):
            cx=(frac(i*.61803398875)-.5)*aspect
            cy=frac(i*.754877666)-.5
            r=(.035+.11*frac(i*.414213562))*(.6+style.pattern_size)
            w=(1-smooth(r*(.25+.5*style.spread),r,xp.hypot(x-cx,y-cy)))*.7
            result=mix(result,bright if i%2==0 else (.91,.57,.29),w*(.4+.6*style.density))
    elif m=="texturedPaper":
        grain=frac(xp.sin(x*n*37.1)*xp.cos(y*n*29.7)*437.17)
        folds=.5+.5*xp.sin(x*7+y*4)
        result=mix(mix(base,(1,.95,.82),.65),dark,.13*grain+.12*folds)
    elif m=="lightTunnel":
        d=xp.maximum(xp.abs(x),xp.abs(y))
        rings=xp.abs(frac(xp.log(d+.025)*(2+style.density*5)/(.5+style.pattern_size))-.5)
        light=1-smooth(.025,.10+.10*style.spread,rings)
        result=mix(dark,bright,light*(.3+.7*(1-xp.minimum(d,1))))
    elif m=="adaptiveColors":
        a,b=colors if colors is not None else (base,bright)
        result=mix(mix(b,(0,0,0),.25),a,gauss(-.3,-.2,.25+.9*style.spread))
    else:
        raise ValueError("Unsupported Canvas backdrop")
    return xp.clip(mix(base,result,style.intensity*style.opacity),0,1)


def make_styled_background(style, width, height, source, colors=None):
    cp=cuda()
    source=rgba(source)
    # Match native bounded procedural texture, then resize the opaque result.
    scale=min(1.,640/max(width,height))
    w,h=max(1,int(width*scale+.5)),max(1,int(height*scale+.5))
    rgb=style_pixels(style,w,h,source,colors,cp)
    rgb=(cp.floor(rgb*255+.5)/255).astype(cp.float32)
    if (w,h)!=(width,height):
        from cupyx.scipy.ndimage import zoom
        rgb=zoom(rgb,(height/h,width/w,1),order=1,prefilter=False)
    out=cp.empty((height,width,4),dtype=cp.float32)
    out[...,:3]=rgb
    out[...,3]=1
    return out
