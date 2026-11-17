# Issue #71 — desktop / Agent 4 GPU caption metadata contract

This is a source-only handoff for the existing Salad renderer. The desktop **never** transcribes, aligns, adds demo words to caption tracks, or requires a local Whisper install.

## Saved native project and ZIP

The project writer round-trips top-level extra properties. The offline ZIP packer copies the saved `concat.json` without changing these fields.

`captionPreset` (new, undoable project-wide styling, valid before any timed captions exist):

```json
{
  "version": 1,
  "presetId": "cuda-caption.preset-2-yellow-bar",
  "generateOnGpu": true,
  "sourceClipId": "c7",
  "style": {
    "fontFamily": "Inter",
    "fontWeight": 900,
    "fontSize": 0.053,
    "color": "#000000",
    "background": "#ffff00",
    "maxWidth": 0.88
  },
  "position": {
    "anchor": "bottom-center",
    "offsetX": 0.0,
    "offsetY": 0.35,
    "safeMargin": 0.08
  }
}
```

The real `style` is the full native `TextStyle` representation with the **content, karaokeWords and karaokeWord fields removed**: it is never speech text or sample word timing. Font selection and text/background colors are editable locally before Apply. Existing user captions retain individual manually positioned clip offsets and exact cue/word timing.

If a narration source exists, one atomic Apply-to-All also sets the existing `gpuCaptionRequest`: version 1; valid `sourceClipId`; `model: "small"`; `wordTimestamps: true`; `renderAfter: true`; serialized `style`; legacy `preset` of `purple-word`, `yellow-bar`, or `classic` for other gallery styles. That shape passes the existing worker request validator. The worker already caches ASR independently from preset/style. Clicking Apply with no actual caption clips is normal; only audio/video source is required for a *runnable* request.

If **no narration/audio exists**, `captionPreset.generateOnGpu=true` and `sourceClipId=null` persist the intent; no invalid `gpuCaptionRequest` is sent to the current worker. UI correctly says narration is still required before GPU generation.

## Agent 4 worker integration / acceptance

1. Worker should read the saved `captionPreset.position` and use **bottom-center** safe-area anchoring for every preset. In particular, replace Yellow Bar worker's historical `offsetY=-0.35` (top) with `+0.35` (bottom). Preserve explicitly user-positioned caption clips.
2. Honor serialized style fields when creating actual captions, including font, font weight/size, fill/background colors, padding/rounding, outlines, max-width and karaoke. Worker remains responsible for audio-derived cue timings and optional word alignment.
3. Preserve recognition cache when changing only style/preset; never use the desktop demo's words as transcription.
4. When the saved project intent has no source yet, defer ASR as `Waiting for narration`; activating a valid request once source material arrives remains the responsibility of the queue integration. Never claim captions generated before GPU actually returns them.
5. Verify desktop preview vs NVIDIA worker render on both landscape and portrait media. Do not claim real GPU parity based on headless or source checks alone.

Do **not** change VPS_1 web UI, VPS deploy, OAuth/Drive Bridge or any unrelated worker orchestration for this desktop branch.
