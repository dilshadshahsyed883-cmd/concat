// Original procedural mask. CUDA counterpart lives in creative_transitions.py.
struct Params { intensity: f32, direction: f32, reverse: f32, speed: f32, easing: f32, seed: f32, softness: f32, roughness: f32, fiber: f32, shadow: f32, paper_red: f32, paper_green: f32, paper_blue: f32 }
fn creative_hash(p: vec2<f32>) -> f32 {
    return fract(sin(dot(p,vec2<f32>(127.1,311.7)))*43758.5453);
}
fn transition(input_uv: vec2<f32>, progress: f32) -> vec4<f32> {
    var p=clamp(progress,0.0,1.0);
    var uv=input_uv;
    if(params.direction==1.0){uv.x=1.0-uv.x;}else if(params.direction==2.0){uv=uv.yx;}else if(params.direction==3.0){uv=vec2<f32>(1.0-uv.y,uv.x);}
    if(params.reverse>0.0){uv=vec2<f32>(1.0)-uv;}
    uv.x=clamp(uv.x+params.seed/65535.0*.02,0.0,1.0);
    p=pow(p,1.0/params.speed);if(params.easing>0.0){p=p*p*(3.0-2.0*p);}
    if (p<=0.0) { return from_at(input_uv); }
    if (p>=1.0) { return to_at(input_uv); }
    let k=clamp(params.intensity*.01*params.roughness,0.0,1.0);
    let soft=(.004+.045*k)*params.softness;
    var glow=0.0; var shade=0.0;
    var edge=abs(uv.x-.5)*2.0; let mask=clamp((p-edge)/soft+.5,0.0,1.0);
    let a=from_at(input_uv); let b=to_at(input_uv);
    let aw=a.a*(1.0-mask); let bw=b.a*mask; let alpha=aw+bw;
    var rgb=vec3<f32>(0.0);
    if(alpha>0.0) { rgb=(a.rgb*aw+b.rgb*bw)/alpha; }
    return vec4<f32>(clamp(rgb+vec3<f32>(params.paper_red,params.paper_green,params.paper_blue)*glow-vec3<f32>(params.shadow*shade),vec3<f32>(0.0),vec3<f32>(1.0)),alpha);
}
