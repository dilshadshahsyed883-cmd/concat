"""Deterministic normalized procedural Canvas backgrounds.

Render uses CuPy arrays throughout. NumPy may be supplied only by source
contract tests to evaluate the identical arithmetic without CUDA hardware.
The same geometry, sample grid, colors and quantization are in concat-core.
"""
from math import pi, floor

STYLES = ("ambientClouds", "frostedGlass", "radialSpotlight", "halftoneArena", "diagonalLightStreaks", "monochromeBackdrop", "topographicWaves", "carbonFiber", "prismRefraction", "cinematicBokeh", "texturedPaper", "lightTunnel")


def render_pattern(xp, pixels, source_size, pattern, fitted_rect):
    height, width = pixels.shape[:2]
    aspect = width / height
    limit = 480 if pattern.uses_source else 960
    scale = min(1., limit / max(width, height))
    w = max(1, floor(width * scale + .5))
    h = max(1, floor(height * scale + .5))
    f = xp.float32
    u, v = xp.meshgrid((xp.arange(w, dtype=f)+.5)/w,
                       (xp.arange(h, dtype=f)+.5)/h)
    x, y = (u-.5)*aspect, v-.5
    b, a = xp.array(pattern.color,dtype=f), xp.array(pattern.accent,dtype=f)
    frequency = (4.+pattern.density*28.)/(.5+pattern.size*1.5)
    edge = .018+pattern.softness*.09
    spread = .12+pattern.spread*.88
    strength = pattern.intensity*pattern.opacity

    def fract(value):
        return value-xp.floor(value)

    def smooth(lo,hi,value):
        t = xp.clip((value-lo)/xp.maximum(hi-lo,.00001),0,1)
        return t*t*(3-2*t)

    def mix(first,second,t):
        t=xp.clip(t,0,1)
        if getattr(t,"ndim",0)==2: t=t[...,None]
        return first*(1-t)+second*t

    def bilinear(texture,sx,sy,left=0,top=0,sw=None,sh=None):
        th,tw=texture.shape[:2]
        sw=tw if sw is None else sw;sh=th if sh is None else sh
        sx=xp.clip(sx,left,left+sw-1);sy=xp.clip(sy,top,top+sh-1)
        x0=xp.floor(sx).astype(xp.int32);y0=xp.floor(sy).astype(xp.int32)
        x1=xp.minimum(x0+1,left+sw-1);y1=xp.minimum(y0+1,top+sh-1)
        fx=sx-x0;fy=sy-y0
        return mix(mix(texture[y0,x0],texture[y0,x1],fx),mix(texture[y1,x0],texture[y1,x1],fx),fy)

    if pattern.uses_source:
        left,top,sw,sh=fitted_rect(*source_size,width,height)
        source_aspect=source_size[0]/source_size[1]
        # Alpha is composed over the background base before interpolation.
        source=pixels[...,:3]*pixels[...,3:4]+b*(1-pixels[...,3:4])
        def sample(su,sv):
            sx=left+((su-.5)*min(1.,aspect/source_aspect)+.5)*sw-.5
            sy=top+((sv-.5)*min(1.,source_aspect/aspect)+.5)*sh-.5
            return bilinear(source,sx,sy,left,top,sw,sh)

    style=STYLES.index(pattern.style)
    colored=None
    if style==0:
        value=.5+.24*xp.sin(x*4/spread+1.7*xp.sin(y*4))+.18*xp.cos(y*5/spread-x*3)+.08*xp.sin((x+y)*8)
    elif style==1:
        offset=.006+pattern.softness*.065
        blur=sum(sample(u+dx*offset,v+dy*offset)/9 for dy in (-1,0,1) for dx in (-1,0,1))
        gx,gy=fract(u*3),fract(v*2)
        inner=xp.minimum(xp.minimum(gx,1-gx),xp.minimum(gy,1-gy))
        panel=smooth(.035,.08,inner)
        rim=1-smooth(.018,.055,xp.abs(inner-.055))
        colored=mix(mix(b,blur,.58),a,.16+.18*panel+.13*rim)
    elif style==2:
        value=xp.exp(-(x*x+y*y)/(spread*spread*.42))
    elif style==3:
        dx,dy=fract(x*frequency)-.5,fract(y*frequency)-.5
        radius=.10+.27*(1-v)
        value=(1-smooth(radius-edge,radius+edge,xp.sqrt(dx*dx+dy*dy)))*(.25+.75*u)
    elif style==4:
        streak=xp.abs(fract((x+y*.75)*frequency*.25)-.5)
        value=xp.exp(-streak*streak/(.001+spread*.012))*(.35+.65*(1-v))
    elif style==5:
        p=sample(u,v);l=p[...,0]*.2126+p[...,1]*.7152+p[...,2]*.0722
        colored=xp.repeat((l*.52)[...,None],3,axis=-1)
    elif style==6:
        wave=x*.45+y+.11*xp.sin(x*5)+.05*xp.sin((x+y)*10)
        d=xp.abs(fract(wave*frequency*.7)-.5)
        value=1-smooth(.045,.045+edge,d)
    elif style==7:
        ix,iy=xp.floor(x*frequency),xp.floor(y*frequency)
        dx,dy=fract(x*frequency),fract(y*frequency)
        horizontal=xp.mod(ix+iy,2)<1
        ridge=xp.where(horizontal,xp.sin(dy*pi),xp.sin(dx*pi))
        value=.12+.72*ridge*ridge
    elif style==8:
        band=xp.floor(u*(4+frequency*.28))
        sign=xp.where(xp.mod(band,2)<1,1.,-1.)
        displacement=sign*(.025+.025*pattern.size)
        p=sample(u+displacement,v+sign*.018)
        r=sample(u+displacement+.009,v);g=sample(u+displacement-.009,v)
        colored=mix(xp.stack((r[...,0],p[...,1],g[...,2]),axis=-1),a,.19)
    elif style==9:
        value=xp.zeros_like(u)
        for cx,cy,r in [(0.08,0.19,0.12),(0.25,0.82,0.11),(0.40,0.13,0.08),(0.72,0.33,0.16),(0.89,0.72,0.14),(0.56,0.86,0.06),(0.13,0.57,0.05),(0.91,0.14,0.08),(0.43,0.45,0.055),(0.65,0.05,0.055)]:
            d=xp.sqrt((u-cx)**2*aspect*aspect+(v-cy)**2)
            r=r*(.5+pattern.size)*spread
            value=xp.maximum(value,(1-smooth(r*(.60-pattern.softness*.35),r,d))*(.45+.55*pattern.density))
    elif style==10:
        gx,gy=xp.floor(u*240),xp.floor(v*240)
        grain=fract(xp.mod(gx*37+gy*17+gx*gy*13,251)/251)
        tear=y+.08*xp.sin(x*12)+.02*xp.sin(x*frequency)
        value=.22+.30*smooth(-.02,.02,tear)+.24*grain*pattern.density
    elif style==11:
        r=xp.maximum(xp.maximum(xp.abs(x),xp.abs(y)),.006)
        ring=xp.abs(fract(-xp.log(r)*frequency*.13)-.5)
        spoke=xp.abs(fract(xp.arctan2(y,x)*4/pi)-.5)
        value=((1-smooth(.06,.06+edge,ring))*.8+(1-smooth(.025,.025+edge,spoke))*.32)*(1-xp.exp(-r*8/spread))
    else:
        raise ValueError("Unknown procedural Canvas style")
    if colored is None: colored=mix(b,a,value)
    # The desktop uploads an RGBA8 texture, so quantize before enlarging.
    texture=(xp.floor(xp.clip(mix(b,colored,strength),0,1)*255+.5)/255).astype(f)
    if (h,w)!=(height,width):
        sx,sy=xp.meshgrid((xp.arange(width,dtype=f)+.5)*w/width-.5,
                          (xp.arange(height,dtype=f)+.5)*h/height-.5)
        texture=bilinear(texture,sx,sy)
    result=xp.empty((height,width,4),dtype=f)
    result[...,:3]=texture
    result[...,3]=1.
    return xp.ascontiguousarray(result)
