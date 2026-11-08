// Soft Golden: native preview uses the same color constants as the CUDA worker.
fn effect(uv: vec2<f32>) -> vec4<f32> {
    let source = sample(uv);
    var rgb = source.rgb;
    rgb = rgb + (source.rgb - soften(uv, 1.0)) * 0.000000;
    rgb = (rgb * exp2(0.000000) - vec3<f32>(0.5)) * 0.960000
        + vec3<f32>(0.5 + 0.035000);
    let luminance = dot(rgb, vec3<f32>(0.2126, 0.7152, 0.0722));
    rgb = mix(vec3<f32>(luminance), rgb, 1.110000);
    rgb.r = rgb.r * (1.0 + 0.25 * 0.500000);
    rgb.b = rgb.b * (1.0 - 0.25 * 0.500000);
    return vec4<f32>(clamp(rgb, vec3<f32>(0.0), vec3<f32>(1.0)), source.a);
}
