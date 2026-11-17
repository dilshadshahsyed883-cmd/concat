"""Deterministic, bounded atmospheric overlays. Video pixels remain CUDA resident.

A 3x3 neighborhood samples an infinite moving cell lattice, so cost is bounded
independently of clip length and the same seed reproduces every frame. The
numeric reference is for deterministic fixtures, never a video-render fallback.
"""
from __future__ import annotations
from dataclasses import dataclass
from functools import cache
from math import cos, sin, floor, exp, sqrt, pi, isfinite
from .contract import cuda, rgba

PRESETS = (
    ("stars", "White Twinkling Stars", 0, (1.,1.,1.), .006, 25., .08),
    ("gold-sparkles", "Golden Sparkles", 1, (1.,.70,.20), .005, 40., .12),
    ("embers", "Floating Orange Embers", 2, (1.,.22,.035), .004, 50., .18),
    ("fireflies", "Rising Fireflies", 3, (.72,1.,.25), .004, 30., .12),
    ("bokeh", "Soft Bokeh Lights", 4, (1.,.68,.35), .028, 18., .04),
    ("dust-motes", "Slow Dust Motes", 5, (.86,.83,.76), .002, 45., .035),
    ("snow", "Snow Particles", 6, (1.,1.,1.), .004, 65., .13),
    ("rain", "Rain Streaks", 7, (.65,.78,1.), .003, 70., .7),
    ("ash", "Soft Floating Ash", 8, (.75,.73,.70), .003, 40., .07),
    ("light-leaks", "Soft Light Leaks", 9, (1.,.45,.12), .04, 8., .025),
    ("shooting-stars", "Shooting Stars", 10, (1.,.94,.78), .003, 10., .5),
    ("confetti", "Falling Confetti", 11, (1.,.40,.12), .006, 55., .2),
)
PARTICLE_IDS = {"concat.particles-"+slug: mode for slug,_,mode,*_ in PRESETS}
# Bounds are also the native manifest contract; no hidden unbounded particle count.
BOUNDS = {"density":(0.,100.), "size":(.001,.04), "size_variance":(0.,100.),
          "speed":(0.,2.), "direction":(-180.,180.), "seed":(0.,65535.),
          "lifetime":(.2,60.), "fade":(0.,10.), "opacity":(0.,1.),
          "red":(0.,1.), "green":(0.,1.), "blue":(0.,1.), "glow":(0.,2.),
          "twinkle":(0.,10.), "points":(4.,6.), "start":(0.,86400.), "end":(0.,86400.)}

def defaults(ident):
    for slug,_,_,color,size,density,speed in PRESETS:
        if ident == "concat.particles-"+slug:
            return {"density":density,"size":size,"size_variance":60.,"speed":speed,
                    "direction":90. if slug in ("snow","rain","confetti") else -90.,
                    "seed":1.,"lifetime":8.,"fade":.7,"opacity":.65,"red":color[0],
                    "green":color[1],"blue":color[2],"glow":.5,"twinkle":2. if slug in ("stars","gold-sparkles","fireflies") else 0.,
                    "points":4.,"start":0.,"end":86400.}
    raise ValueError("Unknown particle preset")

def _value(key, value):
    lo,hi = BOUNDS[key]
    if type(value) not in (float,int) or not isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"Particle {key} must be within {lo}–{hi}")
    if key in ("seed","points") and (int(value)!=value or (key=="points" and value not in (4,6))):
        raise ValueError(f"Particle {key} needs an integer supported value")
    return float(value)

@dataclass(frozen=True)
class ParticleEffect:
    id: str
    params: tuple
    keys: tuple
    start: float
    duration: float
    def settings(self, frame):
        values=dict(self.params)
        if self.keys:
            from .project import ease
            fraction=(frame/30-self.start)/self.duration
            for name,points in self.keys:
                value=points[0][1]
                for i,point in enumerate(points):
                    if fraction < point[0]:
                        if i:
                            a=points[i-1]; w=(fraction-a[0])/(point[0]-a[0]); w=ease(w,point[2]) if point[2] is not None else w
                            value=a[1]+(point[1]-a[1])*w
                        break
                    value=point[1]
                lo,hi=BOUNDS[name];values[name]=min(hi,max(lo,value))
        return values

def parse_effect(effect, start, duration):
    ident=effect.get("id"); values=defaults(ident); supplied=effect.get("params",{})
    if not isinstance(supplied,dict) or set(supplied)-set(BOUNDS): raise ValueError("Unknown particle parameters")
    values.update({key:_value(key,value) for key,value in supplied.items()})
    if values["end"] <= values["start"]: raise ValueError("Particle end must follow start")
    raw=effect.get("keys",{})
    if not isinstance(raw,dict) or set(raw)-set(BOUNDS): raise ValueError("Unknown particle keyframe parameter")
    keys=[]
    for name,points in raw.items():
        if name in ("seed","points","start","end"): raise ValueError(f"Particle {name} cannot be keyframed")
        if not isinstance(points,list) or len(points)>2048: raise ValueError("Too many particle keys")
        parsed=[]; seen=set()
        for point in points:
            at=point.get("at") if isinstance(point,dict) else None
            if type(at) not in (int,float) or not isfinite(at) or not 0<=at<=1 or at in seen: raise ValueError("Invalid particle keyframe position")
            curve=point.get("ease")
            if curve is not None and curve not in ("linear","in","out","inOut"):
                if not isinstance(curve,list) or len(curve)!=4 or any(type(c) not in (float,int) or not isfinite(c) for c in curve): raise ValueError("Invalid particle easing")
                curve=tuple(curve)
            if curve=="linear": curve=None
            parsed.append((float(at),_value(name,point.get("value")),curve));seen.add(at)
        if parsed: keys.append((name,tuple(sorted(parsed))))
    return ParticleEffect(ident,tuple(values.items()),tuple(keys),start,duration)

def _hash(x,y,seed,salt):
    h=((x&0xffffffff)*1664525+(y&0xffffffff)*1013904223+int(seed)*374761393+salt*668265263)&0xffffffff
    h=((h^(h>>13))*1274126177)&0xffffffff
    return (h>>8)/16777216.

def _shape(mode,x,y,t,phase,points):
    r2=x*x+y*y
    if mode in (0,1):
        star=exp(-abs(x*y)*16-sqrt(r2)*1.5)
        if points==6: star=max(star,exp(-abs((.5*x+.8660254*y)*(-.8660254*x+.5*y))*16-sqrt(r2)*1.5))
        return star
    if mode==4: return exp(-r2*2)*.5+exp(-abs(sqrt(r2)-.8)*12)*.3
    if mode in (7,10): return exp(-x*x*25-y*y*.12)
    if mode==9: return exp(-r2*.25)*.35
    if mode==11:
        a=t+phase*6.2831853;u=x*cos(a)-y*sin(a);v=x*sin(a)+y*cos(a)
        return max(0.,min(1.,(1.-abs(u))*8))*max(0.,min(1.,(.45-abs(v))*8))
    return exp(-r2*3)

def sample_reference(ident, params, uv, seconds, aspect=16/9):
    """Small numeric fixture oracle, deliberately not a frame renderer."""
    mode=PARTICLE_IDS[ident];p=defaults(ident);p.update(params)
    if p["density"]<=0 or seconds<p["start"] or seconds>=p["end"]: return (0.,0.,0.)
    t=seconds-p["start"];grid=2+6*p["density"]/100;angle=p["direction"]*pi/180
    qx=uv[0]*grid*aspect-cos(angle)*t*p["speed"];qy=uv[1]*grid-sin(angle)*t*p["speed"]
    bx,by=floor(qx),floor(qy); glow=0.
    for oy in (-1,0,1):
        for ox in (-1,0,1):
            x,y=bx+ox,by+oy;h=_hash(x,y,p["seed"],0);h2=_hash(x,y,p["seed"],1)
            phase=((t+h*p["lifetime"])/p["lifetime"])%1
            env=1. if p["fade"]==0 else min(1.,phase*p["lifetime"]/p["fade"],(1-phase)*p["lifetime"]/p["fade"])
            radius=p["size"]*grid*(1+p["size_variance"]/100*(h2-.5))
            dx=(qx-x-.2-.6*h)/radius;dy=(qy-y-.2-.6*h2)/radius
            twinkle=1. if p["twinkle"]==0 else .55+.45*sin(t*p["twinkle"]*6.2831853+h*6.2831853)
            glow+=(_shape(mode,dx,dy,t,h,p["points"])+p["glow"]*.15*exp(-(dx*dx+dy*dy)*.5))*env*twinkle
    amount=min(1.,max(0.,glow*p["opacity"]))
    return tuple(p[c]*amount for c in ("red","green","blue"))

_SOURCE = r'''
__device__ float phash(int x,int y,float seed,unsigned int salt) {
 unsigned int h=(unsigned int)x*1664525u+(unsigned int)y*1013904223u+(unsigned int)seed*374761393u+salt*668265263u;
 h=(h^(h>>13))*1274126177u; return (h>>8)/16777216.f;
}
extern "C" __global__ void particle_overlay(const float4* src,float4* out,int width,int height,int mode,
 float t,float density,float size,float variance,float speed,float direction,float seed,float lifetime,float fade,
 float opacity,float red,float green,float blue,float glow,float twinkle,float points) {
 long long i=(long long)blockIdx.x*blockDim.x+threadIdx.x;if(i>=(long long)width*height)return;
 float grid=2.f+6.f*density/100.f,angle=direction*.01745329252f;
 float qx=((i%width)+.5f)/height*grid-cosf(angle)*t*speed;
 float qy=((i/width)+.5f)/height*grid-sinf(angle)*t*speed;
 int bx=(int)floorf(qx),by=(int)floorf(qy);float amount=0.f;
 for(int oy=-1;oy<=1;oy++)for(int ox=-1;ox<=1;ox++){
  int x=bx+ox,y=by+oy;float h=phash(x,y,seed,0),h2=phash(x,y,seed,1);
  float phase=t/lifetime+h;phase-=floorf(phase);
  float env=fade<=0.f?1.f:fminf(1.f,fminf(phase*lifetime/fade,(1.f-phase)*lifetime/fade));
  float radius=size*grid*(1.f+variance/100.f*(h2-.5f));
  float dx=(qx-x-.2f-.6f*h)/radius,dy=(qy-y-.2f-.6f*h2)/radius,r2=dx*dx+dy*dy;
  float shape=expf(-r2*3.f);
  if(mode==0||mode==1){shape=expf(-fabsf(dx*dy)*16.f-sqrtf(r2)*1.5f);
   if(points>5.f)shape=fmaxf(shape,expf(-fabsf((.5f*dx+.8660254f*dy)*(-.8660254f*dx+.5f*dy))*16.f-sqrtf(r2)*1.5f));}
  else if(mode==4)shape=expf(-r2*2.f)*.5f+expf(-fabsf(sqrtf(r2)-.8f)*12.f)*.3f;
  else if(mode==7||mode==10)shape=expf(-dx*dx*25.f-dy*dy*.12f);
  else if(mode==9)shape=expf(-r2*.25f)*.35f;
  else if(mode==11){float a=t+h*6.2831853f,u=dx*cosf(a)-dy*sinf(a),v=dx*sinf(a)+dy*cosf(a);
   shape=fminf(1.f,fmaxf(0.f,(1.f-fabsf(u))*8.f))*fminf(1.f,fmaxf(0.f,(.45f-fabsf(v))*8.f));}
  float blink=twinkle<=0.f?1.f:.55f+.45f*sinf(t*twinkle*6.2831853f+h*6.2831853f);
  amount+=(shape+glow*.15f*expf(-r2*.5f))*env*blink;
 }
 amount=fminf(1.f,fmaxf(0.f,amount*opacity));float4 c=src[i];
 out[i]=make_float4(c.x+red*amount,c.y+green*amount,c.z+blue*amount,c.w);
}
'''
@cache
def _kernel(): return cuda().RawKernel(_SOURCE,"particle_overlay")

def apply(pixels, effect: ParticleEffect, frame):
    cp=cuda();source=rgba(pixels);p=effect.settings(frame);seconds=frame/30
    if p["density"]==0 or p["opacity"]==0 or not p["start"]<=seconds<p["end"]:return source
    height,width=source.shape[:2];out=cp.empty_like(source)
    names=("density","size","size_variance","speed","direction","seed","lifetime","fade","opacity","red","green","blue","glow","twinkle","points")
    _kernel()(((width*height+255)//256,), (256,), (source,out,cp.int32(width),cp.int32(height),cp.int32(PARTICLE_IDS[effect.id]),
        cp.float32(seconds-p["start"]),*(cp.float32(p[k]) for k in names)))
    return out
