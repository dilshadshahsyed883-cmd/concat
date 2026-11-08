// Original procedural editor preview. Salad renders the CUDA equivalent.
struct Params { intensity: f32 }
fn procedural_hash(p: vec2<f32>) -> f32 {
    return fract(sin(dot(p,vec2<f32>(127.1,311.7)))*43758.5453);
}
fn transition(uv: vec2<f32>, progress: f32) -> vec4<f32> {
    let p=clamp(progress,0.0,1.0);
    let k=clamp(params.intensity*.01,0.0,1.0);
    let peak=1.0-abs(2.0*p-1.0);
    let cell=procedural_hash(floor(uv*vec2<f32>(24.0,18.0)));
    let grain=procedural_hash(floor(uv*vec2<f32>(240.0,160.0))+vec2<f32>(floor(p*37.0),0.0));
    let dx=uv.x-.5;
    let dy=uv.y-.5;
    let radius=length(uv-vec2<f32>(.5,.5));
    var mask=p;
    var out_uv=uv;
    var in_uv=uv;
    var glow=0.0;
    var tint=vec3<f32>(1.0,1.0,1.0);
    let e=uv.y+.045*sin(uv.x*95.0+p*17.0)*k; mask=clamp((p-e)*18.0+.5,0.0,1.0); glow=exp(-abs(p-e)*75.0)*.5*k; tint=vec3<f32>(.3,1.0,.45);
    let a=from_at(clamp(out_uv,vec2<f32>(0.0),vec2<f32>(1.0)));
    let b=to_at(clamp(in_uv,vec2<f32>(0.0),vec2<f32>(1.0)));
    let blended=mix(a,b,clamp(mask,0.0,1.0));
    var rgb=blended.rgb;
    
    return vec4<f32>(clamp(rgb+glow*tint,vec3<f32>(0.0),vec3<f32>(1.0)),blended.a);
}
