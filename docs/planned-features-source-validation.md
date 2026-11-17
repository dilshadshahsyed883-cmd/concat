# Planned Concat features — source integration

Integration branch: `integration/planned-features-source-20261117`; draft PR #56.

## Implemented source

- #53: versioned editable C Builder profiles, duration/distribution curves, deterministic variation, pins, preview/regeneration and managed profile import/export. Media Selection has independent image/video keyword checkboxes, available counts, 0–100% sliders and a video enable switch. Shares are normalized among selected keywords independently of asset count. Unchecked and zero-weight keywords are excluded. Slider selections persist in the project profile and drive preview and generation.
- #55: atomic ripple insert, explicit overwrite, keep-slot/ripple replacement including file-picker media, independent magnetic and snap settings, gap closure, undo/redo and linked-media safety checks.
- #43–44: native and CUDA timed purple word backing and editable yellow strip caption styles, preserving exact word intervals and editable phrase clips.
- #45–47: native editable sports/orange/portrait-blur canvas presets with non-destructive foreground, frame, banner and headline components; idempotent reapply and worker preflight.
- #48–49: verified repository animation inventory, 18 canvas-motion variants, parameter controls, seeded variation and bulk/favorite application with manual-key protection.
- #50–51: 12 procedural particle overlays and eight creative transition packages, native WGSL and CUDA source, editable parameters and deterministic scalar fixtures. Existing catalogue entries remain available.
- #52: optional CUDA faster-whisper worker recognition, true word timestamps, cache, authenticated JSON/SRT/VTT artifacts, desktop request/import and caption-only queue completion. No guessed word alignment or CPU video rendering fallback.

## Verification

Local Rust source tests: concat-project schema/commands/serialization (183), real C Builder harness (10), concat-text glyph tests (27), concat-effects unit tests (75) plus creative-pack integration test (1). Full Slint tree compiles in a dedicated source harness. Worker suite: 180 tests executed with 11 explicit hardware/runtime skips (with Rust-generated canvas fixtures enabled; otherwise that cross-language test also skips). Source fixture assertions are not measured GPU output.

Source-only GitHub checks also typecheck the complete native Rust/Slint library and test PowerShell contracts. Record their final run results in PR #56; earlier successful runs on the initial snapshot do not verify later commits.

## Remaining acceptance checks

No Windows interactive session or NVIDIA GPU is attached. Real ASR inference, CUDA kernel compilation, shader/native parity, NVENC rendering, interactive drag/undo/save/reopen and Windows queue integration require hardware validation. Preserve issues as open until their required acceptance checks are satisfied. The animation inventory is limited to source actually present in this repository; no external Animation Lab inventory was supplied. Page curl is a procedural curved-edge reveal.

No EXE, Docker/image, installer or release builds, image publication, deployment, main merge or VPS changes were performed. Issue #54 remains excluded. Native canvas companion clips are editable components, never merged or flattened tracks.

## Left-side layout controls follow-up

The Animations library now has Animations, Transform and Canvas sub-tabs. Push In, Pan Right and Fade In share the existing animation/keyframe system and live beside the physics and canvas-motion families. Their parameter controls moved into the left panel.

Transform supports independent image/video targeting, selected/whole-timeline scope, scale, X/Y, rotation and fit/fill. Bulk application protects authored keyframe fields, locks, hidden tracks and canvas companions. Optional single-clip live/keyframe editing preserves the original manual editing path. Canvas background and canvas-preset application share independent image/video targeting. Plain backgrounds affect the main picture lane; existing framed layouts are edited through their preset controls rather than silently changing an invisible foreground background.

Issue #57 was explicitly requested as planning-only and has no implementation under this follow-up. Issue #54 remains excluded.
