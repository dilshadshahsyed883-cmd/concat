struct Params { density: f32, size: f32, size_variance: f32, speed: f32, direction: f32, seed: f32, lifetime: f32, fade: f32, opacity: f32, red: f32, green: f32, blue: f32, glow: f32, twinkle: f32, points: f32, start: f32, end: f32 }
fn particle_hash(cell: vec2<i32>, seed: f32, salt: u32) -> f32 {
    var h = bitcast<u32>(cell.x)*1664525u + bitcast<u32>(cell.y)*1013904223u + u32(seed)*374761393u + salt*668265263u;
    h = (h ^ (h >> 13u))*1274126177u;
    return f32(h >> 8u)/16777216.0;
}
fn effect(uv: vec2<f32>) -> vec4<f32> {
    let c=sample(uv);
    if (params.density<=0.0 || frame.time<params.start || frame.time>=params.end) { return c; }
    let t=frame.time-params.start;
    let grid=2.0+6.0*params.density/100.0;
    let angle=params.direction*0.01745329252;
    let q=uv*vec2<f32>(frame.size.x/max(frame.size.y,1.0)*grid,grid)-vec2<f32>(cos(angle),sin(angle))*t*params.speed;
    let base=vec2<i32>(floor(q));
    var amount=0.0;
    for (var oy:i32=-1;oy<=1;oy++) { for (var ox:i32=-1;ox<=1;ox++) {
        let cell=base+vec2<i32>(ox,oy);
        let h=particle_hash(cell,params.seed,0u); let h2=particle_hash(cell,params.seed,1u);
        let phase=fract(t/params.lifetime+h);
        var envelope=1.0;
        if (params.fade>0.0) { envelope=min(1.0,min(phase*params.lifetime/params.fade,(1.0-phase)*params.lifetime/params.fade)); }
        let radius=params.size*grid*(1.0+params.size_variance/100.0*(h2-0.5));
        let d=(q-vec2<f32>(cell)-vec2<f32>(0.2)-0.6*vec2<f32>(h,h2))/radius;
        let r2=dot(d,d);
        var shape=exp(-r2*3.0);
        let mode=7;
        if (mode==0 || mode==1) {
            shape=exp(-abs(d.x*d.y)*16.0-sqrt(r2)*1.5);
            if (params.points>5.0) { shape=max(shape,exp(-abs((0.5*d.x+0.8660254*d.y)*(-0.8660254*d.x+0.5*d.y))*16.0-sqrt(r2)*1.5)); }
        } else if (mode==4) { shape=exp(-r2*2.0)*0.5+exp(-abs(sqrt(r2)-0.8)*12.0)*0.3; }
        else if (mode==7 || mode==10) { shape=exp(-d.x*d.x*25.0-d.y*d.y*0.12); }
        else if (mode==9) { shape=exp(-r2*0.25)*0.35; }
        else if (mode==11) {
            let a=t+h*6.2831853; let u=d.x*cos(a)-d.y*sin(a); let v=d.x*sin(a)+d.y*cos(a);
            shape=clamp((1.0-abs(u))*8.0,0.0,1.0)*clamp((0.45-abs(v))*8.0,0.0,1.0);
        }
        var blink=1.0;
        if (params.twinkle>0.0) { blink=0.55+0.45*sin(t*params.twinkle*6.2831853+h*6.2831853); }
        amount+=(shape+params.glow*0.15*exp(-r2*0.5))*envelope*blink;
    } }
    amount=clamp(amount*params.opacity,0.0,1.0);
    return vec4<f32>(c.rgb+vec3<f32>(params.red,params.green,params.blue)*amount,c.a);
}
