"""Native rounded rectangles and source masks, composited entirely on CUDA.

Small numeric reference functions are deterministic geometry fixtures, never
pixel/video fallbacks. Unsupported native shape/mask families fail validation.
"""
from dataclasses import dataclass
from functools import cache
from math import cos, sin, radians, hypot, isfinite
from .contract import cuda, rgba
from .canvas import fitted_rect


def number(value, label, low, high):
    if type(value) not in (int, float) or not isfinite(value) or not low <= value <= high:
        raise ValueError(f"Unsupported GPU canvas {label}")
    return float(value)


def color(value, label, *, empty=False):
    if empty and value == "": return (0., 0., 0., 0.)
    if not isinstance(value, str) or len(value) not in (7,9) or not value.startswith('#'):
        raise ValueError(f"Invalid canvas {label} color")
    try: raw = bytes.fromhex(value[1:])
    except ValueError as exc: raise ValueError(f"Invalid canvas {label} color") from exc
    return tuple(c/255 for c in (*raw, 255)[:4]) if len(raw)==3 else tuple(c/255 for c in raw)


@dataclass(frozen=True)
class Rectangle:
    width: float
    height: float
    radius: float
    fill: tuple
    stroke: tuple
    stroke_width: float


def parse_shape(spec):
    if not isinstance(spec, dict) or set(spec)-{'kind','size','width','height','cornerRadius','fill','stroke','strokeWidth'}:
        raise ValueError("Unsupported GPU canvas shape parameters")
    if spec.get('kind','square') != 'square':
        raise ValueError("GPU canvas supports rectangle shape layers only")
    size = number(spec.get('size',.35),'shape size',.01,2)
    width = number(spec.get('width',0),'shape width',0,2)
    height = number(spec.get('height',0),'shape height',0,2)
    radius = number(spec.get('cornerRadius',0),'shape cornerRadius',0,.5)
    fill = color(spec.get('fill','#ffffff'),'shape fill',empty=True)
    stroke = color(spec.get('stroke',''),'shape stroke',empty=True)
    rule = number(spec.get('strokeWidth',.012),'shape strokeWidth',0,.5)
    # Width=0 is the existing native square's frame-height size.
    return Rectangle(width if width else size*1080/1920, height if height else size,
                     radius,fill,stroke,rule)


@dataclass(frozen=True)
class RectangleMask:
    x: float = 0
    y: float = 0
    width: float = .75
    height: float = .5
    rotation: float = 0
    roundness: float = 0
    feather: float = 0
    invert: bool = False


def parse_mask(spec):
    if spec is None: return None
    if not isinstance(spec,dict) or set(spec)-{'shape','x','y','width','height','rotation','roundness','feather','invert'}:
        raise ValueError("Unsupported GPU canvas mask parameters")
    if spec.get('shape') != 'rectangle': raise ValueError("GPU canvas supports rectangle masks only")
    if type(spec.get('invert',False)) is not bool: raise ValueError("Invalid canvas mask invert")
    return RectangleMask(
        number(spec.get('x',0),'mask x',-2,2), number(spec.get('y',0),'mask y',-2,2),
        number(spec.get('width',.75),'mask width',.01,4), number(spec.get('height',.5),'mask height',.01,4),
        number(spec.get('rotation',0),'mask rotation',-1e6,1e6), number(spec.get('roundness',0),'mask roundness',0,1),
        number(spec.get('feather',0),'mask feather',0,1),spec.get('invert',False))


def rounded_distance(x, y, half_width, half_height, radius):
    radius=max(0,min(radius,half_width,half_height))
    dx,dy=abs(x)-half_width+radius,abs(y)-half_height+radius
    return hypot(max(dx,0),max(dy,0))+min(max(dx,dy),0)-radius


def mask_coverage(spec, u, v, source_width, source_height, pixel=1/1080):
    """The native core::mask rectangular signed-distance formula, for fixtures."""
    short=min(source_width,source_height)
    dx=(u-.5-spec.x)*source_width/short;dy=(v-.5-spec.y)*source_height/short
    a=radians(spec.rotation);x=dx*cos(a)+dy*sin(a);y=-dx*sin(a)+dy*cos(a)
    radius=spec.roundness*min(spec.width,spec.height)/2
    d=rounded_distance(x,y,spec.width/2,spec.height/2,radius)
    band=max(spec.feather*.5,pixel,1e-6)
    t=min(max((d+band*.5)/band,0),1)
    kept=1-t*t*(3-2*t)
    return 1-kept if spec.invert else kept


_SOURCE=r'''
__device__ float rounded_distance(float x,float y,float hw,float hh,float radius) {
    radius=fminf(fmaxf(radius,0.f),fminf(hw,hh));
    float dx=fabsf(x)-hw+radius,dy=fabsf(y)-hh+radius;
    return hypotf(fmaxf(dx,0.f),fmaxf(dy,0.f))+fminf(fmaxf(dx,dy),0.f)-radius;
}
__device__ float smooth(float low,float high,float value) {
    float t=fminf(fmaxf((value-low)/(high-low),0.f),1.f);return t*t*(3.f-2.f*t);
}
extern "C" __global__ void canvas_rectangle(float4* out,int w,int h,float fw,float fh,float radius,
    float fr,float fg,float fb,float fa,float sr,float sg,float sb,float sa,float stroke) {
    long long i=(long long)blockIdx.x*blockDim.x+threadIdx.x;if(i>=(long long)w*h)return;
    float d=rounded_distance((int)(i%w)+.5f-w*.5f,(int)(i/w)+.5f-h*.5f,fw*w*.5f,fh*h*.5f,radius*h);
    float fill=(1.f-smooth(-.5f,.5f,d))*fa;
    float rule=stroke>0.f&&sa>0.f?(1.f-smooth(-.5f,.5f,fabsf(d)-fmaxf(stroke*h,1.f)*.5f))*sa:0.f;
    float alpha=rule+fill*(1-rule);
    out[i]=alpha>0.f?make_float4((sr*rule+fr*fill*(1-rule))/alpha,(sg*rule+fg*fill*(1-rule))/alpha,
        (sb*rule+fb*fill*(1-rule))/alpha,alpha):make_float4(0,0,0,0);
}
extern "C" __global__ void canvas_mask(const float4* src,float4* out,int w,int h,
    float fw,float fh,float mx,float my,float hw,float hh,float co,float si,float radius,float band,int invert) {
    long long i=(long long)blockIdx.x*blockDim.x+threadIdx.x;if(i>=(long long)w*h)return;
    float shortside=fminf(fw,fh);
    float dx=((int)(i%w)+.5f-w*.5f-mx*fw)/shortside;
    float dy=((int)(i/w)+.5f-h*.5f-my*fh)/shortside;
    float d=rounded_distance(dx*co+dy*si,-dx*si+dy*co,hw,hh,radius);
    float kept=1.f-smooth(-band*.5f,band*.5f,d);if(invert)kept=1.f-kept;
    float4 value=src[i];value.w*=kept;out[i]=value;
}
'''
@cache
def _shape_kernel(): return cuda().RawKernel(_SOURCE,'canvas_rectangle')
@cache
def _mask_kernel(): return cuda().RawKernel(_SOURCE,'canvas_mask')


def render_shape(shape, width, height):
    cp=cuda();out=cp.empty((height,width,4),dtype=cp.float32)
    _shape_kernel()(((width*height+255)//256,),(256,),
        (out,cp.int32(width),cp.int32(height),*(cp.float32(x) for x in
         (shape.width,shape.height,shape.radius,*shape.fill,*shape.stroke,shape.stroke_width))))
    return out


def apply_mask(pixels, spec, source_size, scale=1):
    cp=cuda();source=rgba(pixels);h,w=source.shape[:2]
    _,_,fw,fh=fitted_rect(*source_size,w,h);a=radians(spec.rotation)
    band=max(spec.feather*.5,1/(min(fw,fh)*max(scale,.0001)),1e-6)
    out=cp.empty(source.shape,dtype=cp.float32)
    _mask_kernel()(((w*h+255)//256,),(256,),
        (cp.ascontiguousarray(source),out,cp.int32(w),cp.int32(h),
         *(cp.float32(x) for x in (fw,fh,spec.x,spec.y,spec.width/2,spec.height/2,cos(a),sin(a),
             spec.roundness*min(spec.width,spec.height)/2,band)),cp.int32(spec.invert)))
    return out
