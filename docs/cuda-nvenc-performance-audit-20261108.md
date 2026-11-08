# CUDA / NVENC performance audit — 2026-11-08

Scope: Salad RTX 3060 Ti-class renderer, 1080p30 SDR, `cloud_render/concat/hybrid/`.
This document concerns Agent 2 only. Do not confuse CI validation with live NVIDIA throughput.

## Observed baseline

User-observed before-change throughput: approximately **31.70 FPS / 1.06x**.
No live Salad GPU was connected to this implementation session. Accordingly **no
post-change FPS, speedup percentage, visual-equivalence result, or GPU allocation
figure has been measured**. Do not infer throughput from a passed CPU-only test.

## Path audited and conclusions

| Stage | Current behavior | Finding / action |
| --- | --- | --- |
| NVDEC + seek | `Nvdec.decode` indexes device-memory frames, converts planar RGB to CUDA RGBA and synchronizes before decoder surface reuse. | Do **not** remove the safety synchronization without proving decoder lifetime; add exact, bounded reuse of fitted frames at the `Sources.video` call site. |
| CFR/frame selection | Per-source `ffprobe` runs once, then timeline frame index maps to source FPS/speed. | A 24fps source on a 30fps timeline repeats frames. The new 2-entry device-resident LRU avoids decoding/resizing the exact same source index twice while it remains cached. |
| Stills | Baseline JPEG goes through CUDA MJPEG decode + CUDA resize and is retained in an existing 6-entry cache. | Already cached; no new still-conversion path or CPU video fallback. |
| Animations + transitions | CUDA layer transforms and transition kernels are scheduled by the timeline. | Preserve keyframe interpolation and transition semantics; no unverified math rewrite. |
| Filters | CUDA effects and native color kernels produce new surfaces; master-layer filters can require an extra composite. | Potential memory traffic, but raw-kernel fusion requires GPU pixel-diff tests before modification. |
| Compositing | CUDA kernel builds a float32 RGBA canvas per frame. | Existing alpha behavior retained; no compositor edits to avoid conflict with the Canvas agent. |
| Captions | Pillow creates cached glyph masks; CuPy uploads/overlays them on GPU. | CPU **text rasterization**, not CPU video rendering; keep word timing and cue placement intact. |
| RGB→NV12 | Device-side CuPy operations create limited-range BT.709 NV12 for NVENC. | Potential memory bandwidth cost; preserve rounding/chroma and device residency. |
| NVENC | GPU encoder uses a dedicated CUDA stream and waits for input surface lifetime. | No change to stream synchronization, P4 preset, CFR frame ordering or raw H.264 output. |
| Audio | FFmpeg decodes/retimes; CuPy mixes PCM; FFmpeg limits and encodes AAC. | CPU audio codec use is intentional; no CPU **video** fallback; no change to synchronization. |
| Mux/verification | Stream-copy H.264 to MP4 and verify frame count, geometry, FPS, audio timeline. | Retained unchanged; stage duration is now optionally logged. |

## Implemented optimization

- A 2-entry LRU of **fitted** 1080p CUDA RGBA frames, keyed by `(path, source_frame)`.
- At 1920 × 1080 × 4 channels × float32, the two cached surfaces are approximately
  63.3 MiB combined; evict the oldest key when a third is needed.
- Pure identity reuse: downstream effects operate on their own outputs; cache hits return
  the same source pixels without recomputing or changing the frame index.
- CPU fixture for five 30fps timeline frames using a 24fps video: **5 → 4**
  mock NVDEC/fit calls, a **20% reduction in repeated calls in that fixture only**.
  This is not a measured improvement in end-to-end frames per second.

## Diagnostic profiling

```bash
CONCAT_CUDA_PROFILE=1 python -m cloud_render.concat.hybrid.render \
  /absolute/project/folder /absolute/output/profile-run.mp4
```

Stages recorded: project validation, NVDEC/fit, scene assembly (includes nested
decode and effects), compositing, captions, NV12 packing, NVENC, mux/audio and output
verification. Frame samples start at frame 1 and occur every 120 frames; frame 0
excludes JIT startup. Per-stage reports show sample count, mean/max elapsed ms and
source-frame cache hits/misses.

**Diagnostic results must not be used as throughput measurements.** Frame stages
synchronize the CUDA stream at entry/exit to include device work; this alters
scheduling and can increase render time. Nested stages overlap in accounting.

## Reproducible live before/after benchmark (not yet executed)

1. Use a **single pinned RTX 3060 Ti-class Salad instance**, NVIDIA driver, image
   dependencies, FFmpeg and PyNvVideoCodec versions, input files, timeline, bitrates
   and output settings. Use a genuinely mixed video + image timeline with captions,
   transitions, animations, master filter layer and audio.
2. In two separate clean source checkouts, use baseline commit
   `77fd6601b49caece5a91b693fdb64c8b08da0316` and the
   `work/concat-gpu-performance-20261108` PR head. Avoid benchmarking while an
   unrelated Canvas/manifest change is integrated into only one side.
3. Set `CONCAT_CUDA_PROFILE=0` for both. Warm each path once, then run at least
   three full renders with unique output names, and record wall seconds, frames,
   speed multiplier `(frames/30)/wall_seconds`, and output file metadata.
4. Compare medians, not only the fastest run. Use `ffprobe` to verify identical
   frame count, 1920 × 1080 / 30fps, H.264 SDR and AAC duration/sync, plus decoded
   pixel/quality checks against baseline. A codec-specific bitstream hash need not
   match if output pixels are visually equivalent.
5. Repeat with `CONCAT_CUDA_PROFILE=1` only **after** measuring uninstrumented
   throughput, to identify whether NVDEC, source effects, compositing, captions,
   NV12 conversion, NVENC or audio is the limiting stage.

Do not claim a speedup or enable aggressive CUDA fusion until the live paired
benchmark and visual/audio fidelity checks pass.

## Verification

GitHub Linux CI for PR #31: 143 unittest cases, 18 skipped (including
CUDA-hardware-dependent tests), overall PASS. Unit tests prove opt-in diagnostics
and bounded repeat-frame behavior. The live mixed-media GPU export/visual benchmark
remains outstanding; CI tests alone cannot prove faster NVIDIA rendering.
