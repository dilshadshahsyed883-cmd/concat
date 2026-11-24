//! Versioned, deterministic Canvas background parameters and native texture synthesis.
use serde::{Deserialize, Serialize};
use concat_core::frame::Frame;

/// Stable mode order shared with the background selector.
pub const MODES: [&str; 17] = ["source","blur","color","none","ambientClouds","frostedGlass","radialSpotlight","halftoneArena","diagonalStreaks","monochrome","topographicWaves","carbonFiber","prismRefraction","cinematicBokeh","texturedPaper","lightTunnel","adaptiveColors"];
/// True for the versioned procedural and source-derived modes.
pub fn is_styled(mode: &str) -> bool { MODES[4..].contains(&mode) }

/// Independent background controls. Old profiles get these defaults.
#[cfg_attr(feature = "schema", derive(schemars::JsonSchema))]
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase", deny_unknown_fields)]
pub struct Controls {
    /// Effect strength.
    pub intensity: f64,
    /// Texture repetition density.
    pub density: f64,
    /// Blend with the base color.
    pub opacity: f64,
    /// Light radius.
    pub spread: f64,
    /// Pattern cell scale.
    pub pattern_size: f64,
}
impl Default for Controls {
    fn default() -> Self { Self { intensity: 0.75, density: 0.5, opacity: 1., spread: 0.5, pattern_size: 0.5 } }
}
impl Controls {
    /// All controls must be normalized finite values.
    pub fn valid(&self) -> bool {
        [self.intensity,self.density,self.opacity,self.spread,self.pattern_size].iter().all(|v| v.is_finite() && (0. ..=1.).contains(v))
    }
}
/// Metadata owned by the generated backdrop clip, not the foreground.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Style {
    /// Schema version.
    pub version: u32,
    /// Stable mode identifier.
    pub mode: String,
    /// Base RGB color.
    pub color: String,
    /// Frosted source blur.
    pub softness: f64,
    /// Independent parameters.
    pub controls: Controls,
}
impl Style {
    /// Reject unknown or invalid style metadata.
    pub fn valid(&self) -> bool {
        self.version == 1 && is_styled(&self.mode) && rgb(&self.color).is_some()
            && self.softness.is_finite() && (0. ..=1.).contains(&self.softness) && self.controls.valid()
    }
    /// Read only an owned backdrop.
    pub fn of(clip: &crate::model::Clip) -> Option<Self> {
        if clip.extra.get("canvasRole")?.as_str()? != "backdrop" || !clip.extra.contains_key("canvasOwner") { return None; }
        let style: Self = serde_json::from_value(clip.extra.get("canvasBackdropV1")?.clone()).ok()?;
        style.valid().then_some(style)
    }
    /// Modes that sample the current source rather than a fixed texture.
    pub fn dynamic(&self) -> bool { matches!(self.mode.as_str(), "frostedGlass" | "monochrome" | "prismRefraction") }
}
type Rgb = [f64; 3];
fn rgb(s: &str) -> Option<Rgb> {
    if !s.starts_with('#') || !matches!(s.len(), 7|9) || !s[1..].bytes().all(|b| b.is_ascii_hexdigit()) { return None; }
    Some([u8::from_str_radix(&s[1..3],16).ok()? as f64/255.,u8::from_str_radix(&s[3..5],16).ok()? as f64/255.,u8::from_str_radix(&s[5..7],16).ok()? as f64/255.])
}
fn mix(a: Rgb,b: Rgb,t:f64)->Rgb { let t=t.clamp(0.,1.); std::array::from_fn(|i|a[i]*(1.-t)+b[i]*t) }
fn fract(x:f64)->f64{x-x.floor()}
fn smooth(a:f64,b:f64,x:f64)->f64{let t=((x-a)/(b-a)).clamp(0.,1.);t*t*(3.-2.*t)}
fn gauss(x:f64,y:f64,cx:f64,cy:f64,r:f64)->f64{(-((x-cx).powi(2)+(y-cy).powi(2))/r.powi(2)).exp()}
fn sample(frame:&Frame,u:f64,v:f64)->Rgb{
    let x=(u.clamp(0.,1.)*f64::from(frame.width().saturating_sub(1))).round() as u32;
    let y=(v.clamp(0.,1.)*f64::from(frame.height().saturating_sub(1))).round() as u32;
    let p=frame.pixel(x,y).unwrap_or([0,0,0,0]); std::array::from_fn(|i|f64::from(p[i])/255.*f64::from(p[3])/255.)
}
/// Stable dominant palette, quantized 4x4x4; equal counts use the lower bin.
pub fn palette(frame:&Frame)->[[f64;3];2]{
    let mut counts=[0u32;64]; let mut sums=[[0.;3];64];
    for j in 0..32 {for i in 0..32 {
        let c=sample(frame,(i as f64+0.5)/32.,(j as f64+0.5)/32.);
        let q=c.map(|v|(v*4.).floor().min(3.) as usize);
        let k=q[0]*16+q[1]*4+q[2];counts[k]+=1;
        for n in 0..3{sums[k][n]+=c[n];}
    }}
    let mut order:Vec<_>=(0..64).collect();order.sort_by_key(|&k|(std::cmp::Reverse(counts[k]),k));
    let first=order[0];let second=if counts[order[1]]>0{order[1]}else{first};
    [first,second].map(|k|sums[k].map(|v|v/f64::from(counts[k].max(1))))
}
/// Normalized style pixel. Source sampling covers, rather than stretches, its frame.
pub fn pixel(style:&Style,u:f64,v:f64,aspect:f64,source:Option<&Frame>,colors:Option<[[f64;3];2]>)->Rgb{
    let base=rgb(&style.color).unwrap_or([0.;3]);
    let c=&style.controls; let strength=c.intensity; let n= (5.+25.*c.density)/(0.4+1.2*c.pattern_size);
    let x=(u-0.5)*aspect;let y=v-0.5;
    let bright=mix(base,[1.,1.,1.],0.7);let dark=mix(base,[0.;3],0.75);
    let src=|u:f64,v:f64|{source.map(|f|{
        let a=f64::from(f.width())/f64::from(f.height().max(1));
        sample(f,0.5+(u-0.5)*(aspect/a).min(1.),0.5+(v-0.5)*(a/aspect).min(1.))
    }).unwrap_or(base)};
    let result=match style.mode.as_str(){
        "ambientClouds" => {let r=0.3+0.7*c.spread;let g=gauss(x,y,-0.35,-0.15,r);let h=gauss(x,y,0.4,0.25,r);mix(mix(dark,bright,g*0.8),[0.38,0.24,0.68],h*0.6)},
        "frostedGlass" => {let d=0.012+0.075*style.softness;let mut sum=[0.;3];for j in -2..=2{for i in -2..=2{let p=src(u+f64::from(i)*d/aspect,v+f64::from(j)*d);for k in 0..3{sum[k]+=p[k]/25.;}}}let t=fract(u*3.);let pane=if (0.04..0.96).contains(&t){0.28}else{0.58};mix(sum,bright,pane)},
        "radialSpotlight" => mix(dark,bright,gauss(x,y,0.,-0.08,0.18+0.85*c.spread)),
        "halftoneArena" => {let dx=fract(x*n)-0.5;let dy=fract(y*n)-0.5;let fade=(1.-v).clamp(0.,1.);mix(dark,bright,(1.-smooth(0.11+fade*0.17,0.15+fade*0.17,dx.hypot(dy)))*0.9)},
        "diagonalStreaks" => {let t=fract((x+y*0.75)*(n*0.25));let band=1.-smooth(0.025,0.08+0.3*c.spread,(t-0.5).abs());mix(dark,bright,band*(0.35+0.65*(1.-v)))},
        "monochrome" => {let p=src(u,v);let l=(p[0]*0.2126+p[1]*0.7152+p[2]*0.0722)*0.6;[l;3]},
        "topographicWaves" => {let z= y+0.11*(x*5.).sin()+0.06*(x*11.+y*6.).sin();let t=(fract(z*n*0.55)-0.5).abs();mix(dark,bright,1.-smooth(0.035,0.095,t))},
        "carbonFiber" => {let ix=(x*n).floor();let iy=(y*n).floor();let f=if (ix+iy).rem_euclid(2.)<1.{fract(x*n)}else{fract(y*n)};let v=0.1+0.22*(std::f64::consts::PI*f).sin().powi(2);mix(dark,bright,v)},
        "prismRefraction" => {let cell=(u*n*0.4).floor();let shift=(cell*1.7).sin()*0.075;let a=src(u+shift-0.012,v);let b=src(u+shift,v);let d=src(u+shift+0.012,v);mix([a[0],b[1],d[2]],bright,0.12+0.13*fract(u*n*0.4))},
        "cinematicBokeh" => {let mut col=dark;for i in 0..12{let k=f64::from(i);let cx=(fract(k*0.61803398875)-0.5)*aspect;let cy=fract(k*0.754877666)-0.5;let r=(0.035+0.11*fract(k*0.414213562))* (0.6+c.pattern_size);let d=(x-cx).hypot(y-cy);let w=(1.-smooth(r*(0.25+0.5*c.spread),r,d))*0.7;col=mix(col,if i%2==0{bright}else{[0.91,0.57,0.29]},w*(0.4+0.6*c.density));}col},
        "texturedPaper" => {let grain=fract((x*n*37.1).sin()*(y*n*29.7).cos()*437.17);let folds=0.5+0.5*(x*7.+y*4.).sin();mix(mix(base,[1.,0.95,0.82],0.65),dark,0.13*grain+0.12*folds)},
        "lightTunnel" => {let d=x.abs().max(y.abs());let rings=(fract((d+0.025).ln()*(2.+c.density*5.)/(0.5+c.pattern_size))-0.5).abs();let light=1.-smooth(0.025,0.10+0.10*c.spread,rings);mix(dark,bright,light*(0.3+0.7*(1.-d.min(1.))))},
        "adaptiveColors" => {let [a,b]=colors.unwrap_or([base,bright]);let w=gauss(x,y,-0.3,-0.2,0.25+0.9*c.spread);mix(mix(b,[0.;3],0.25),a,w)},
        _=>base
    };
    mix(base,result,strength*c.opacity)
}
/// Generate a bounded native texture; the compositor scales it to the canvas.
pub fn texture(style:&Style,width:u32,height:u32,source:Option<&Frame>,colors:Option<[[f64;3];2]>)->Option<Frame>{
    if !style.valid() || width==0 || height==0 {return None;}
    let scale=(640./f64::from(width.max(height))).min(1.);
    let w=(f64::from(width)*scale).round().max(1.) as u32;
    let h=(f64::from(height)*scale).round().max(1.) as u32;
    let aspect=f64::from(width)/f64::from(height);
    let mut bytes=Vec::with_capacity(w as usize*h as usize*4);
    for y in 0..h {for x in 0..w {
        let p=pixel(style,(f64::from(x)+0.5)/f64::from(w),(f64::from(y)+0.5)/f64::from(h),aspect,source,colors);
        bytes.extend(p.map(|v|(v.clamp(0.,1.)*255.).round() as u8));bytes.push(255);
    }}
    Frame::from_rgba(w,h,bytes)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn canvas_styles_are_distinct_and_bounded() {
        let mut source=Frame::black(32,16);for y in 0..16{for x in 0..32{source.set_pixel(x,y,[x as u8*8,y as u8*16,90,255]);}}
        let palette=palette(&source);let mut outputs=Vec::new();
        for mode in &MODES[4..]{
            let style=Style{version:1,mode:(*mode).into(),color:"#18334f".into(),softness:0.6,controls:Controls::default()};
            let tex=texture(&style,64,36,Some(&source),Some(palette)).unwrap();
            assert_eq!(tex.width(),64);assert_eq!(tex.height(),36);assert!(tex.pixels().chunks_exact(4).all(|p|p[3]==255));
            assert!(!outputs.contains(&tex.pixels().to_vec()),"duplicate mode {mode}");outputs.push(tex.pixels().to_vec());
            let mut off=style.clone();off.controls.opacity=0.;assert_eq!(pixel(&off,0.2,0.3,1.5,Some(&source),Some(palette)),rgb(&style.color).unwrap());
        }
    }
    #[test]
    fn canvas_palette_is_deterministic() {
        let mut f=Frame::black(16,16);f.fill([200,30,90,255]);let p=palette(&f);
        assert_eq!(p[0],p[1]);assert!((p[0][0]-200./255.).abs()<1e-12);
    }
}
