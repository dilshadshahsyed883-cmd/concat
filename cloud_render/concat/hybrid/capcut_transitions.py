"""CUDA-resident, original procedural transitions inspired by editing patterns.

The built-in Concat catalog and native GPU worker share the stable IDs below.
No CapCut assets, proprietary code, or CPU rendering fallback are used.
"""
from __future__ import annotations

from functools import cache
from math import isfinite

from .contract import cuda

CAPCUT_PRESETS = (
    ("ink-drying", "Ink Drying"),
    ("shake-flash", "Shake & Flash"),
    ("image-blend", "Image Blend"),
    ("torn-paper", "Torn Paper"),
    ("matrix-scan", "Matrix Scan"),
    ("ghost-radio", "Ghost Radio"),
    ("explosion-glitch", "Explosion Glitch"),
    ("comic-cut", "Comic Cut"),
    ("red-chaos", "Red Chaos"),
    ("cyber-beehive", "Cyber Beehive"),
    ("heat-flash", "Heat Flash"),
    ("shards-scatter", "Shards Scatter"),
    ("violet-fuzz", "Violet Fuzz"),
    ("warp-portal", "Warp Portal"),
    ("starlink-fence", "Starlink Fence"),
    ("liquid-melt", "Liquid Melt"),
    ("burning-flame", "Burning Flame"),
    ("pulse-wipe", "Pulse Wipe"),
    ("twinkle-zoom", "Twinkle Zoom"),
    ("wispy-wipe", "Wispy Wipe"),
)
CAPCUT_KINDS = {slug: index for index, (slug, _) in enumerate(CAPCUT_PRESETS)}
CAPCUT_IDS = {"concat." + slug: slug for slug, _ in CAPCUT_PRESETS}
DEFAULT_INTENSITY = 0.70

_CUDA_SOURCE = r"""
__device__ __forceinline__ float sat(float x) {
    return fminf(1.f, fmaxf(0.f, x));
}
__device__ __forceinline__ float rand2(float x, float y) {
    float v = sinf(x * 127.1f + y * 311.7f) * 43758.5453f;
    return v - floorf(v);
}
__device__ __forceinline__ float4 fetch(
    const float4 *src, float u, float v, int w, int h
) {
    int x = min(w - 1, max(0, (int)floorf(sat(u) * (w - 1) + .5f)));
    int y = min(h - 1, max(0, (int)floorf(sat(v) * (h - 1) + .5f)));
    return src[(long long)y * w + x];
}
__device__ __forceinline__ float4 blend(float4 a, float4 b, float mix_amount) {
    float t = sat(mix_amount);
    float aw = a.w * (1.f - t), bw = b.w * t, alpha = aw + bw;
    if (alpha <= 0.f) return make_float4(0.f, 0.f, 0.f, 0.f);
    return make_float4((a.x*aw+b.x*bw)/alpha, (a.y*aw+b.y*bw)/alpha,
                       (a.z*aw+b.z*bw)/alpha, alpha);
}
extern "C" __global__ void capcut_transition(
    const float4 *outgoing, const float4 *incoming, float4 *output,
    int width, int height, int style, float progress, float intensity
) {
    long long index = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= (long long)width * height) return;
    int x = (int)(index % width), y = (int)(index / width);
    float u = (x + .5f) / width, v = (y + .5f) / height;
    float p = sat(progress), k = sat(intensity), peak = 1.f - fabsf(2.f*p - 1.f);
    float mask = p, ux = u, uy = v, vx = u, vy = v;
    float highlight = 0.f, red = 1.f, green = 1.f, blue = 1.f;
    float cell = rand2(floorf(u*24.f), floorf(v*18.f));
    float grain = rand2(floorf(u*240.f)+floorf(p*37.f), floorf(v*160.f));
    float dx = u - .5f, dy = v - .5f;
    float radial = sqrtf(dx*dx + dy*dy);
    if (style == 0) {  // Ink Drying: irregular radial ink blot grows outward.
        mask = sat((p*1.55f - radial*1.9f - (cell-.5f)*.21f)*13.f+.5f);
    } else if (style == 1) {  // Shake & Flash
        float shake = sinf(82.f*p + v*110.f)*.045f*peak*k;
        ux += shake; vx -= shake;
        highlight = powf(peak, 8.f)*1.15f*k;
    } else if (style == 2) {  // Image Blend
        mask = p*p*(3.f-2.f*p);
        ux = .5f+dx/(1.f+.08f*p*k);
        vx = .5f+dx/(1.f+.08f*(1.f-p)*k);
    } else if (style == 3) {  // Torn Paper
        float edge = u + (rand2(floorf(v*45.f), 2.f)-.5f)*.16f*k;
        mask = sat((p-edge)*20.f+.5f);
        highlight = expf(-fabsf(p-edge)*70.f)*.22f*k;
    } else if (style == 4) {  // Matrix Scan
        float edge = v + .045f*sinf(u*95.f+p*17.f)*k;
        mask = sat((p-edge)*18.f+.5f);
        highlight = expf(-fabsf(p-edge)*75.f)*.5f*k;
        red=.3f; green=1.f; blue=.45f;
    } else if (style == 5) {  // Ghost Radio
        float jump = sinf(v*240.f+p*71.f)*.05f*k*peak;
        ux += jump; vx -= jump;
        highlight = (grain>.9f ? .18f : 0.f)*k*peak;
        red=.65f; green=.85f; blue=1.f;
    } else if (style == 6) {  // Explosion Glitch
        float burst = (cell-.5f)*.25f*k*peak;
        ux += burst; vy -= burst*.75f;
        mask = sat(p + (cell-.5f)*.7f*peak*k);
        highlight = (grain>.96f ? .3f : 0.f)*peak*k;
    } else if (style == 7) {  // Comic Cut
        float dot = (sinf(u*200.f)*sinf(v*200.f)+1.f)*.5f;
        mask = sat((p-u)*12.f+.5f + (dot-.5f)*k*.5f);
    } else if (style == 8) {  // Red Chaos
        float jitter = (grain-.5f)*.18f*k*peak;
        ux += jitter; vx -= jitter;
        mask = sat(p+(cell-.5f)*peak*k);
        highlight = (grain>.77f ? .32f : 0.f)*peak*k;
        red=1.f; green=.2f; blue=.2f;
    } else if (style == 9) {  // Cyber Beehive: offset hex-cell mosaic
        float row = floorf(v*15.f), col = floorf(u*22.f+(fmodf(row,2.f)*.5f));
        float h = rand2(col,row);
        mask = sat((p-h)*12.f+.5f);
        highlight = fabsf(p-h)<.035f ? .18f*k : 0.f;
        red=.3f; green=1.f; blue=1.f;
    } else if (style == 10) {  // Heat Flash
        float wave = sinf(u*24.f+p*30.f)*.018f*peak*k;
        uy += wave; vy -= wave;
        highlight = powf(peak,5.f)*.65f*k;
        red=1.f; green=.63f; blue=.22f;
    } else if (style == 11) {  // Shards Scatter
        float shard = rand2(floorf(u*14.f+v*5.f),floorf(v*13.f));
        mask = sat((p-shard)*12.f+.5f);
        ux += (shard-.5f)*.2f*peak*k;
        vy += (shard-.5f)*.2f*peak*k;
    } else if (style == 12) {  // Violet Fuzz
        float static_noise = (grain-.5f)*.7f*k*peak;
        mask = sat(p+static_noise);
        highlight = (grain>.83f ? .28f : 0.f)*peak*k;
        red=.8f; green=.25f; blue=1.f;
    } else if (style == 13) {  // Warp Portal
        float twist = 2.5f*k*peak;
        float c=cosf(twist*radial*3.f), s=sinf(twist*radial*3.f);
        ux=.5f+dx*c-dy*s; uy=.5f+dx*s+dy*c;
        vx=.5f+dx*c+dy*s; vy=.5f-dx*s+dy*c;
        mask = sat((p*1.5f-radial*1.6f)*10.f+.5f);
    } else if (style == 14) {  // Starlink Fence
        float stripe = fmodf(fabsf((u+v*.55f)*22.f),1.f);
        mask = sat((p-u)*9.f+.5f+(stripe-.5f)*k*.8f);
    } else if (style == 15) {  // Liquid Melt
        float drip = .13f*sinf(u*25.f+p*8.f)*k + .07f*(cell-.5f)*k;
        mask = sat((p-v+drip)*12.f+.5f);
        vy += (1.f-p)*.08f*k*sinf(u*31.f);
    } else if (style == 16) {  // Burning Flame
        float flicker = .17f*sinf(u*41.f+p*37.f)*k+.08f*(cell-.5f)*k;
        float edge = 1.f-v + flicker;
        mask = sat((p-edge)*15.f+.5f);
        highlight = expf(-fabsf(p-edge)*55.f)*.55f*k;
        red=1.f; green=.46f; blue=.05f;
    } else if (style == 17) {  // Pulse Wipe
        float edge = radial*1.6f;
        mask = sat((p-edge)*13.f+.5f);
        highlight = expf(-fabsf(p-edge)*38.f)*.2f*k;
    } else if (style == 18) {  // Twinkle Zoom
        float scale = 1.f+.3f*peak*k;
        ux=.5f+dx/scale; uy=.5f+dy/scale;
        vx=.5f+dx*scale; vy=.5f+dy*scale;
        highlight = (grain>.975f ? .55f : 0.f)*peak*k;
    } else {  // Wispy Wipe
        float wisp = .11f*sinf(v*24.f+p*15.f)*k + .05f*(cell-.5f)*k;
        mask = sat((p-u+wisp)*14.f+.5f);
        highlight = expf(-fabsf(p-u+wisp)*55.f)*.13f*k;
    }
    float4 a = fetch(outgoing,ux,uy,width,height);
    float4 b = fetch(incoming,vx,vy,width,height);
    float4 result = blend(a,b,mask);
    if (style == 7) {  // Comic panel colour blocking.
        result.x = (1.f-k*peak)*result.x+k*peak*floorf(result.x*5.f+.5f)/5.f;
        result.y = (1.f-k*peak)*result.y+k*peak*floorf(result.y*5.f+.5f)/5.f;
        result.z = (1.f-k*peak)*result.z+k*peak*floorf(result.z*5.f+.5f)/5.f;
    }
    result.x = sat(result.x + highlight*red);
    result.y = sat(result.y + highlight*green);
    result.z = sat(result.z + highlight*blue);
    output[index] = result;
}
"""


@cache
def _kernel():
    return cuda().RawKernel(_CUDA_SOURCE, "capcut_transition")


def render_capcut_transition(outgoing, incoming, kind: str, progress: float,
                             intensity: float = DEFAULT_INTENSITY):
    """CUDA-only RGBA effect; callers validate surfaces and exact endpoints."""
    if kind not in CAPCUT_KINDS:
        raise ValueError(f"Unknown procedural transition: {kind}")
    if type(progress) not in (int, float) or not isfinite(progress) or not 0 <= progress <= 1:
        raise ValueError("Transition progress must be finite and in [0, 1]")
    if type(intensity) not in (int, float) or not isfinite(intensity) or not 0 <= intensity <= 1:
        raise ValueError("Transition intensity must be finite and in [0, 1]")
    if outgoing.shape != incoming.shape:
        raise ValueError("Transition surfaces must have equal dimensions")
    if progress <= 0:
        return outgoing
    if progress >= 1:
        return incoming
    cp = cuda()
    height, width, channels = outgoing.shape
    if channels != 4:
        raise ValueError("Transition requires RGBA surfaces")
    dst = cp.empty(outgoing.shape, dtype=cp.float32)
    _kernel()(
        ((width * height + 255)//256,), (256,),
        (cp.ascontiguousarray(outgoing), cp.ascontiguousarray(incoming), dst,
         cp.int32(width), cp.int32(height), cp.int32(CAPCUT_KINDS[kind]),
         cp.float32(progress), cp.float32(intensity)),
    )
    return dst
