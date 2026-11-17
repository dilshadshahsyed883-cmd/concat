"""Original creative reveal masks shared by the catalogue and CUDA worker.

The scalar mask evaluator is a deterministic test oracle, never a video-rendering
backend. Rendered video surfaces remain CUDA resident throughout this module.
"""
from __future__ import annotations
from functools import cache
from math import atan2, cos, exp, floor, hypot, isfinite, pi, sin
from .contract import cuda, rgba

CREATIVE_PRESETS = (
    ('diagonal-wipe', 'Diagonal Wipe'),
    ('door-reveal', 'Center Door Reveal'),
    ('paper-rip-vertical', 'Vertical Paper Rip'),
    ('paper-rip-diagonal', 'Diagonal Paper Rip'),
    ('double-paper-tear', 'Double Paper Tear'),
    ('paper-unfold', 'Crumpled Paper Unfold'),
    ('brush-stroke', 'Brush Stroke Reveal'),
    ('page-curl', 'Page Curl Reveal'),
)
CREATIVE_KINDS = {slug: index for index, (slug, _) in enumerate(CREATIVE_PRESETS)}
# Reuse the existing clock catalogue package, preserving its original identifier.
CREATIVE_KINDS['clock-wipe'] = len(CREATIVE_KINDS)
CREATIVE_IDS = {'concat.' + slug: slug for slug in CREATIVE_KINDS}


CONTROL_DEFAULTS = {"direction":0.,"reverse":0.,"speed":1.,"easing":0.,"seed":0.,
    "softness":1.,"roughness":1.,"fiber":0.,"shadow":1.,"paper_red":1.,"paper_green":1.,"paper_blue":1.}
CONTROL_BOUNDS = {"direction":(0.,3.),"reverse":(0.,1.),"speed":(.25,4.),"easing":(0.,1.),"seed":(0.,65535.),
    "softness":(.1,4.),"roughness":(0.,2.),"fiber":(0.,1.),"shadow":(0.,2.),
    "paper_red":(0.,1.),"paper_green":(0.,1.),"paper_blue":(0.,1.)}
def controls(value=None):
    if value is None: return dict(CONTROL_DEFAULTS)
    if not isinstance(value,dict) or set(value)-set(CONTROL_DEFAULTS): raise ValueError("Unknown creative transition controls")
    out=dict(CONTROL_DEFAULTS)
    for key,v in value.items():
        lo,hi=CONTROL_BOUNDS[key]
        if type(v) not in (int,float) or not isfinite(v) or not lo<=v<=hi: raise ValueError("Invalid creative transition "+key)
        if key in ("direction","reverse","easing") and int(v)!=v: raise ValueError("Transition switch must be integral")
        out[key]=float(v)
    return out

def _sat(v): return min(1., max(0., v))
def _hash(x, y):
    value = sin(x * 127.1 + y * 311.7) * 43758.5453
    return value-floor(value)


def mask_at(kind, u, v, progress, intensity=.7, width=1920, height=1080, params=None):
    """Scalar fixture oracle: foreground coverage, with exact endpoint semantics."""
    _validate(kind, progress, intensity)
    if not all(type(x) in (int,float) and isfinite(x) and 0 <= x <= 1 for x in (u,v)):
        raise ValueError('Mask coordinates must be finite fractions')
    if type(width) is not int or type(height) is not int or min(width,height)<1:
        raise ValueError('Mask dimensions must be positive integers')
    p, k = progress, intensity
    if p<=0: return 0.
    if p>=1: return 1.
    cfg=controls(params)
    if cfg["direction"]==1:u=1-u
    elif cfg["direction"]==2:u,v=v,u
    elif cfg["direction"]==3:u,v=1-v,u
    if cfg["reverse"]:u,v=1-u,1-v
    p=p**(1/cfg["speed"])
    if cfg["easing"]:p=p*p*(3-2*p)
    u=min(1,max(0,u+cfg["seed"]/65535*.02))
    k=min(1,max(0,k*cfg["roughness"]))
    soft=(.004+.045*k)*cfg["softness"]
    if kind=='diagonal-wipe': edge=(u+v)*.5; return _sat((p-edge)/soft+.5)
    if kind=='door-reveal': edge=abs(u-.5)*2; return _sat((p-edge)/soft+.5)
    if kind=='paper-rip-vertical': edge=v+(_hash(floor(u*56),3)-.5)*.18*k
    elif kind=='paper-rip-diagonal': edge=(u+v)*.5+(_hash(floor((u-v)*48),7)-.5)*.19*k
    elif kind=='double-paper-tear': edge=abs(u-.5)*2+(_hash(floor(v*52),11)-.5)*.2*k
    elif kind=='paper-unfold':
        folds=abs(sin(u*25.132741))*abs(sin(v*18.849556))
        edge=max(abs(u-.5),abs(v-.5))*2+(folds-.5)*.22*k
    elif kind=='brush-stroke':
        bristle=(_hash(floor(v*140),17)-.5)*.14*k
        edge=u+sin(v*16)*.085*k+bristle
    elif kind=='page-curl': edge=u+.2*sin(v*pi)*k
    elif kind=='clock-wipe':
        x,y=(u-.5)*width,(v-.5)*height
        angle=atan2(x,-y)
        if angle<0: angle+=2*pi
        radius=hypot(x,y)
        eased=p*p*(3-2*p)
        return _sat((eased*2*pi-angle)*radius+.5)*_sat(angle*radius+.5)
    else: raise ValueError('Unknown creative transition')
    edge += cfg["fiber"]*sin(v*512+cfg["seed"])*.01
    return _sat((p*(1.0+.22*k)-edge)/soft+.5)


def _validate(kind, progress, intensity):
    if kind not in CREATIVE_KINDS: raise ValueError('Unknown creative transition')
    if any(type(v) not in (int,float) or not isfinite(v) or not 0<=v<=1 for v in (progress,intensity)):
        raise ValueError('Transition progress and intensity must be finite fractions')


_CUDA_SOURCE = r'''
__device__ float cr_sat(float x) { return fminf(1.f,fmaxf(0.f,x)); }
__device__ float cr_hash(float x,float y) { float r=sinf(x*127.1f+y*311.7f)*43758.5453f;return r-floorf(r); }
extern "C" __global__ void creative_transition(const float4* a,const float4* b,float4* out,
    int width,int height,int style,float p,float k,
    float direction,float reverse,float speed,float easing,float seed,float softness,float roughness,float fiber,float shadow,
    float paper_red,float paper_green,float paper_blue) {
    long long i=(long long)blockIdx.x*blockDim.x+threadIdx.x;
    if(i>=(long long)width*height)return;
    float u=((i%width)+.5f)/width,v=((i/width)+.5f)/height;
    if(direction==1.f)u=1.f-u;else if(direction==2.f){float old=u;u=v;v=old;}else if(direction==3.f){float old=u;u=1.f-v;v=old;}
    if(reverse>0.f){u=1.f-u;v=1.f-v;}
    p=powf(p,1.f/speed);if(easing>0.f)p=p*p*(3.f-2.f*p);
    u=cr_sat(u+seed/65535.f*.02f);k=cr_sat(k*roughness);
    float soft=(.004f+.045f*k)*softness,edge=0.f,glow=0.f,shade=0.f,mask=0.f;
    if(style==0) {edge=(u+v)*.5f;mask=cr_sat((p-edge)/soft+.5f);}
    else if(style==1) {edge=fabsf(u-.5f)*2.f;mask=cr_sat((p-edge)/soft+.5f);}
    else if(style==2) {edge=v+(cr_hash(floorf(u*56.f),3.f)-.5f)*.18f*k;}
    else if(style==3) {edge=(u+v)*.5f+(cr_hash(floorf((u-v)*48.f),7.f)-.5f)*.19f*k;}
    else if(style==4) {edge=fabsf(u-.5f)*2.f+(cr_hash(floorf(v*52.f),11.f)-.5f)*.2f*k;}
    else if(style==5) {float folds=fabsf(sinf(u*25.132741f))*fabsf(sinf(v*18.849556f));edge=fmaxf(fabsf(u-.5f),fabsf(v-.5f))*2.f+(folds-.5f)*.22f*k;}
    else if(style==6) {float bristle=(cr_hash(floorf(v*140.f),17.f)-.5f)*.14f*k;edge=u+sinf(v*16.f)*.085f*k+bristle;}
    else if(style==7) {edge=u+.2f*sinf(v*3.14159265359f)*k;}
    else {float x=(u-.5f)*width,y=(v-.5f)*height;float angle=atan2f(x,-y);if(angle<0.f)angle+=6.28318530718f;float radius=sqrtf(x*x+y*y);float eased=p*p*(3.f-2.f*p);mask=cr_sat((eased*6.28318530718f-angle)*radius+.5f)*cr_sat(angle*radius+.5f);}
    if(style>=2&&style<=7) {
        edge+=fiber*sinf(v*512.f+seed)*.01f;
        float distance=p*(1.f+.22f*k)-edge;
        mask=cr_sat(distance/soft+.5f);
        if(style<=5) {glow=expf(-fabsf(distance)*100.f)*.13f*k;shade=expf(-fabsf(distance-.018f)*75.f)*.09f*k;}
        if(style==7) {glow=expf(-fabsf(distance)*30.f)*.5f*k;shade=expf(-fabsf(distance+.04f)*18.f)*.3f*k;}
    }
    float4 aa=a[i],bb=b[i];float aw=aa.w*(1.f-mask),bw=bb.w*mask,alpha=aw+bw;
    float3 rgb=alpha>0.f?make_float3((aa.x*aw+bb.x*bw)/alpha,(aa.y*aw+bb.y*bw)/alpha,(aa.z*aw+bb.z*bw)/alpha):make_float3(0.f,0.f,0.f);
    out[i]=make_float4(cr_sat(rgb.x+paper_red*glow-shadow*shade),cr_sat(rgb.y+paper_green*glow-shadow*shade),cr_sat(rgb.z+paper_blue*glow-shadow*shade),alpha);
}
'''

@cache
def _kernel(): return cuda().RawKernel(_CUDA_SOURCE,'creative_transition')


def render_creative_transition(outgoing, incoming, kind, progress, intensity=.7, params=None):
    """Blend two unmodified CUDA surfaces; exact endpoints do not allocate a frame."""
    _validate(kind,progress,intensity)
    cfg=controls(params)
    if kind=="clock-wipe" and params:raise ValueError("Advanced controls are not supported on the legacy clock wipe")
    a,b=rgba(outgoing),rgba(incoming)
    if a.shape!=b.shape: raise ValueError('Transition surfaces must have equal dimensions')
    if progress==0:return a
    if progress==1:return b
    cp=cuda();height,width,_=a.shape;result=cp.empty(a.shape,dtype=cp.float32)
    _kernel()(((width*height+255)//256,),(256,),
        (cp.ascontiguousarray(a),cp.ascontiguousarray(b),result,cp.int32(width),cp.int32(height),
         cp.int32(CREATIVE_KINDS[kind]),cp.float32(progress),cp.float32(intensity),
         *(cp.float32(cfg[key]) for key in CONTROL_DEFAULTS)))
    return result
