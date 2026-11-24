// SPDX-License-Identifier: AGPL-3.0-or-later
//! Resolution-independent procedural canvas textures. Same normalized
//! geometry and source sampling contract as the Salad CUDA renderer.
use crate::Frame;

/// Stable style identifiers, in inspector order.
pub const STYLES: [&str; 12] = ["ambientClouds", "frostedGlass", "radialSpotlight", "halftoneArena", "diagonalLightStreaks", "monochromeBackdrop", "topographicWaves", "carbonFiber", "prismRefraction", "cinematicBokeh", "texturedPaper", "lightTunnel"];
/// Find a stable procedural style identifier.
pub fn style_id(name: &str) -> Option<usize> { STYLES.iter().position(|s| *s == name) }

/// Validated rendering parameters, independent from the framed foreground.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Pattern {
    /// Index in the stable style catalogue.
    pub style: usize,
    /// Base color, in SDR RGB.
    pub color: [u8; 3],
    /// Accent color, in SDR RGB.
    pub accent: [u8; 3],
    /// Effect strength.
    pub intensity: f32,
    /// Pattern frequency.
    pub density: f32,
    /// Edge and source softness.
    pub softness: f32,
    /// Blend over the base.
    pub opacity: f32,
    /// Lighting extent.
    pub spread: f32,
    /// Pattern element scale.
    pub size: f32,
}

fn fract(v: f32) -> f32 { v-v.floor() }
fn smooth(lo: f32, hi: f32, v: f32) -> f32 {
    let t=((v-lo)/(hi-lo).max(0.00001)).clamp(0.,1.);
    t*t*(3.-2.*t)
}
fn mix(a: [f32;3], b: [f32;3], t:f32)->[f32;3] {
    let t=t.clamp(0.,1.);
    std::array::from_fn(|i| a[i]*(1.-t)+b[i]*t)
}
fn rgb(v:[u8;3])->[f32;3] {v.map(|c|f32::from(c)/255.)}

/// Sample the source cover in normalized canvas coordinates, independent
/// of the foreground aperture, zoom and rotation.
fn cover(source: Option<&Frame>, u:f32, v:f32, aspect:f32, base:[f32;3])->[f32;3] {
    let Some(frame)=source else {return base;};
    let w=frame.width().max(1); let h=frame.height().max(1);
    let a=w as f32/h as f32;
    let sx=((u-0.5)*(aspect/a).min(1.)+0.5)*w as f32-0.5;
    let sy=((v-0.5)*(a/aspect).min(1.)+0.5)*h as f32-0.5;
    let x=sx.clamp(0.,(w-1) as f32); let y=sy.clamp(0.,(h-1) as f32);
    let x0=x.floor() as u32; let y0=y.floor() as u32;
    let x1=(x0+1).min(w-1); let y1=(y0+1).min(h-1);
    let fx=x-x0 as f32; let fy=y-y0 as f32;
    let read=|x:u32,y:u32|{
        let bytes=frame.bytes_per_pixel();
        let at=(y as usize*w as usize+x as usize)*bytes;
        let raw=frame.pixels();
        let value=|i:usize| if bytes==4 {raw[at+i] as f32/255.}
            else {u16::from_le_bytes([raw[at+i*2],raw[at+i*2+1]]) as f32/65535.};
        mix(base,[value(0),value(1),value(2)],value(3))
    };
    mix(mix(read(x0,y0),read(x1,y0),fx),mix(read(x0,y1),read(x1,y1),fx),fy)
}
impl Pattern {
    /// Whether a style takes its pixels from the source at each frame.
    pub fn uses_source(self)->bool {matches!(self.style,1|5|8)}
    /// Produce a bounded-resolution texture; the compositor stretches it
    /// to exactly the output canvas. Static textures are cached by the caller.
    pub fn render(self, width:u32,height:u32,source:Option<&Frame>)->Frame {
        let limit=if self.uses_source(){480.}else{960.};
        let scale=(limit/width.max(height).max(1) as f32).min(1.);
        let w=((width as f32*scale).round() as u32).max(1);
        let h=((height as f32*scale).round() as u32).max(1);
        let aspect=width.max(1) as f32/height.max(1) as f32;
        let mut frame=Frame::black(w,h);
        for (i,pixel) in frame.pixels_mut().chunks_exact_mut(4).enumerate() {
            let u=((i as u32%w) as f32+0.5)/w as f32;
            let v=((i as u32/w) as f32+0.5)/h as f32;
            let c=self.sample(u,v,aspect,source);
            for k in 0..3 {pixel[k]=(c[k].clamp(0.,1.)*255.).round() as u8;}
        }
        frame
    }
    /// The shared per-pixel pattern contract. Output is straight SDR RGB.
    pub fn sample(self,u:f32,v:f32,aspect:f32,source:Option<&Frame>)->[f32;3] {
        let b=rgb(self.color); let a=rgb(self.accent);
        let x=(u-0.5)*aspect; let y=v-0.5;
        let frequency=(4.+self.density*28.)/(0.5+self.size*1.5);
        let edge=0.018+self.softness*0.09;
        let spread=0.12+self.spread*0.88;
        let strength=self.intensity*self.opacity;
        let sample=|u,v|cover(source,u,v,aspect,b);
        let mut colored=None;
        let value=match self.style {
            0 => {
                let q=(x*4./spread+1.7*(y*4.).sin()).sin();
                let r=(y*5./spread-x*3.).cos();
                0.5+0.24*q+0.18*r+0.08*((x+y)*8.).sin()
            }
            1 => {
                let offset=0.006+self.softness*0.065;
                let mut blur=[0.;3];
                for dy in [-1.,0.,1.] {for dx in [-1.,0.,1.] {
                    let p=sample(u+dx*offset,v+dy*offset);
                    for k in 0..3 {blur[k]+=p[k]/9.;}
                }}
                let gx=fract(u*3.);let gy=fract(v*2.);
                let inner=gx.min(1.-gx).min(gy.min(1.-gy));
                let panel=smooth(0.035,0.08,inner);
                let rim=1.-smooth(0.018,0.055,(inner-0.055).abs());
                colored=Some(mix(mix(b,blur,0.58),a,(0.16+0.18*panel+0.13*rim).clamp(0.,1.)));
                0.
            }
            2 => (-(x*x+y*y)/(spread*spread*0.42)).exp(),
            3 => {
                let dx=fract(x*frequency)-0.5;let dy=fract(y*frequency)-0.5;
                let radius=0.10+0.27*(1.-v);
                (1.-smooth(radius-edge,radius+edge,(dx*dx+dy*dy).sqrt()))*(0.25+0.75*u)
            }
            4 => {
                let streak=(fract((x+y*0.75)*frequency*0.25)-0.5).abs();
                (-(streak*streak)/(0.001+spread*0.012)).exp()*(0.35+0.65*(1.-v))
            }
            5 => {
                let p=sample(u,v);let l=p[0]*0.2126+p[1]*0.7152+p[2]*0.0722;
                colored=Some([l*0.52;3]); 0.
            }
            6 => {
                let wave=x*0.45+y+0.11*(x*5.).sin()+0.05*((x+y)*10.).sin();
                let d=(fract(wave*frequency*0.7)-0.5).abs();
                1.-smooth(0.045,0.045+edge,d)
            }
            7 => {
                let ix=(x*frequency).floor();let iy=(y*frequency).floor();
                let dx=fract(x*frequency);let dy=fract(y*frequency);
                let horizontal=(ix+iy).rem_euclid(2.)<1.;
                let ridge=if horizontal{(dy*std::f32::consts::PI).sin()}else{(dx*std::f32::consts::PI).sin()};
                0.12+0.72*ridge.powi(2)
            }
            8 => {
                let band=(u*(4.+frequency*0.28)).floor();
                let sign=if band.rem_euclid(2.)<1.{1.}else{-1.};
                let displacement=sign*(0.025+0.025*self.size);
                let p=sample(u+displacement,v+sign*0.018);
                let r=sample(u+displacement+0.009,v);
                let g=sample(u+displacement-0.009,v);
                colored=Some(mix([r[0],p[1],g[2]],a,0.19)); 0.
            }
            9 => {
                let mut glow=0.0f32;
                for (cx,cy,r) in [(0.08,0.19,0.12),(0.25,0.82,0.11),(0.40,0.13,0.08),(0.72,0.33,0.16),(0.89,0.72,0.14),(0.56,0.86,0.06),(0.13,0.57,0.05),(0.91,0.14,0.08),(0.43,0.45,0.055),(0.65,0.05,0.055)] {
                    let d=((u-cx).powi(2)*aspect*aspect+(v-cy).powi(2)).sqrt();
                    let r=r*(0.5+self.size)*spread;
                    glow=glow.max((1.-smooth(r*(0.60-self.softness*0.35),r,d))*(0.45+0.55*self.density));
                }
                glow
            }
            10 => {
                let gx=(u*240.).floor();let gy=(v*240.).floor();
                let grain=fract((gx*37.+gy*17.+gx*gy*13.).rem_euclid(251.)/251.);
                let tear=(y+0.08*(x*12.).sin()+0.02*(x*frequency).sin());
                (0.22+0.30*smooth(-0.02,0.02,tear)+0.24*grain*self.density).clamp(0.,1.)
            }
            11 => {
                let r=x.abs().max(y.abs()).max(0.006);
                let ring=(fract(-r.ln()*frequency*0.13)-0.5).abs();
                let spoke=(fract(y.atan2(x)*4./std::f32::consts::PI)-0.5).abs();
                ((1.-smooth(0.06,0.06+edge,ring))*0.8+(1.-smooth(0.025,0.025+edge,spoke))*0.32)
                    *(1.-(-r*8./spread).exp())
            }
            _=>0.
        };
        mix(b,colored.unwrap_or_else(||mix(b,a,value)),strength)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn all_patterns_are_distinct_and_strength_zero_is_base() {
        let mut source=Frame::black(64,36);
        for (i,p) in source.pixels_mut().chunks_exact_mut(4).enumerate(){p[0]=(i%256) as u8;p[1]=((i/64)*7) as u8;p[2]=150;}
        let mut rendered=Vec::new();
        for style in 0..STYLES.len() {
            let mut p=Pattern{style,color:[16,28,45],accent:[170,190,218],intensity:0.9,density:0.5,softness:0.5,opacity:1.,spread:0.65,size:0.5};
            let frame=p.render(64,36,Some(&source));
            assert!(!rendered.contains(&frame.pixels().to_vec()),"duplicate style {style}");
            rendered.push(frame.pixels().to_vec());
            p.intensity=0.;
            assert!(p.render(64,36,Some(&source)).pixels().chunks_exact(4).all(|c|c==[16,28,45,255]));
        }
    }
}
