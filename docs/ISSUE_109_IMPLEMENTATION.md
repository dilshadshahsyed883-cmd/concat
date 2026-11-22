# Issue 109 implementation and verification

The user authorized implementation on 22 November 2026, then instructed continuous work through matching GPU image and Windows EXE builds. PR #110 uses `issue-109-editor-consistency`; implementation supersedes the historical planning-only authorization. This document distinguishes implemented behavior from platform verification and build evidence.

| Item | Implementation |
| --- | --- |
| 1 | Templates navigation becomes Adjustment. Transform, Mirror/Flip, Canvas, Canvas Presets and Content Zoom/Crop live here; Animations contains motion browsing. Existing template data/backend callbacks remain. |
| 2 | Current authored masks are checked independently of restoration snapshots. Replace Existing Mask / Cancel explicitly confirms exact targets; stale confirmations require a fresh Apply. |
| 3 | Owner deletion removes its generated Canvas companions in the same transaction and Undo step, including ripple edits. |
| 4 | Trim, speed and move edits synchronize companion playback/timing. Owner splitting creates separately owned head/tail groups, deduplicates multi-layer selection and rebinds tail companions. |
| 5 | Reapply uses current source timing/speed; immutable original restoration metadata is retained. |
| 6 | Locked companion tracks are checked on reapply and protected against implicit lifecycle mutations; failure rolls back the whole edit. |
| 7 | Canonical generated-layer snapshots distinguish later manual customizations. Reapply asks before replacing customized or ambiguous legacy layers; Cancel preserves them. |
| 8 | Content keys/transforms compose inside a fixed Canvas frame. Decoration/frame geometry remains independent; genuine keys are preserved. Source masks and the output aperture multiply rather than replace one another. |
| 9–11 | Versioned generated-animation ownership persists with clips through reopen, Undo/Redo and Trim/Split. Manual/ambiguous keys remain protected; all desktop animation callbacks use atomic replacement. |
| 12–13 | Style-only blur/corner updates apply even when movement keys match. Owned corners can update or clear; existing authored masks are never claimed as generated. |
| 14 | Family switching restores only unchanged preset-owned styling; independent user edits remain. |
| 15 | Active motion intervals receive bounded frame-aware sampling. Dense generated vectors bypass manual key hit tolerance so distinct visible-frame samples survive application. |
| 16 | Canvas anchor UI/backend bounds match at −0.45 through +0.45. |
| 17 | Basic / Physics label, vector card illustrations, effective timing/ranges, favorite-card multi-selection and one shared Favorites application inspector. Each chosen preset keeps its own family-specific configured settings in preferences. One selected favorite is valid; an empty pool cannot become All Favorites. |
| 18 | Content Zoom/X/Y operate inside the fixed rounded/rotated opening; Reset Fit restores contain-fit/centering without resetting frame settings. The dedicated content page targets framed videos. Canvas Apply Favorites Randomly uses all starred Canvas layout presets plus shared media switches and existing Bulk scope, without importing animation pool selection. |
| 19 | Common media switches/scope, contextual settings, pinned Apply bars and shared adjustment state. Explicit Selected/All keep their scopes; scope-driven random actions honor Bulk scope. Effects share applicable targeting; captions/transitions retain their own semantics. Reapplying an effect updates its type without duplicates or clearing unrelated types. |

## Render and compatibility contract

- `canvasViewportV1` stores the fixed parent frame/opening; ordinary clip scale/X/Y/rotation and keys are content transforms. Before/after Canvas application orders produce equivalent final framing. Reapply does not accumulate transforms or generated layers.
- The native engine composes content motion with the parent and clips in output coordinates. Both WGPU and the CPU reference multiply the aperture with any source mask. The CUDA worker performs the same composition and clips the final foreground on device.
- Render contract version **2** is required by both Windows helper copies and advertised by the image. This prevents an older worker from silently ignoring viewport geometry. Native document/bundle format version 1 remains readable; the v2 worker also accepts stored contract-v1 ZIPs.
- Arbitrary native crop, stretch, speed curves and other previously unsupported CUDA combinations remain subject to existing validation. Retaining current backdrop playback state does not certify these features as supported by CUDA.
- Ambiguous animations from old projects without reliable ownership metadata remain protected. Legacy Canvas placement migrates only when it still matches generated placement; later manual values remain.

## Verification before release builds

- Rust 1.93.1: **62 core tests** and **260 project tests with schema enabled** passed. The project suite emits native fixtures for worker contracts.
- Worker suite: **222 tests**, **14 skipped** where dependencies/physical CUDA are unavailable; no failures. Covers native viewport transforms, rounded-aperture coverage, dense keys and contract negotiation/legacy recovery.
- Animation/caption and shared-layout source contracts: **16 passed**. Full Slint tree compilation is run separately.
- Native fixed-aperture GPU/reference parity passes with `CONCAT_REQUIRE_GPU=1`: SSIM **0.9999, 0.9994, 0.9997** for different content zoom/rotation cases. This uses the available WGPU adapter, not physical NVIDIA/CUDA acceptance.
- Broad renderer run: 99 passed, one HDR-look test failed. The same `every_look_carries_a_highlight_past_white` failure reproduces on the unchanged parent `4337605f236fcd554fe23372390a753986d7ef3e`; it is recorded as a baseline limitation, not hidden as a passing suite. The added aperture parity case passes independently.
- Full native application typechecking/building is performed by CI; local Linux full-app checking lacks development dependencies. Windows scaling/manual interaction and physical NVIDIA/CUDA acceptance remain separate checks.

## Matching build evidence

The explicit `.github/release-requests/issue-109-build.txt` marker runs the existing Windows and Salad candidate workflows on this branch. Both use the exact event SHA. The Windows bundle embeds source SHA and contract-v2 metadata; the immutable image publishes the matching SHA, digest and startup/contract smoke evidence. Build results and artifact references are recorded on PR #110 after completion. Main-branch publication conditions and live deployment behavior are unchanged.
