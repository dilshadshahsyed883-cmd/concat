# Issue 109 implementation progress

Implementation authorized by the user on 22 November 2026. This supersedes the historical planning-only authorization for the work being implemented. The issue remains open. This branch is an initial implementation batch, not completion of all 19 items or a release request.

## Initial batch

| Issue item | Changes in this branch |
| --- | --- |
| 5 | Canvas reapply builds the source backdrop with current foreground start, duration, source start, speed and speed curve. The original restoration snapshot remains immutable. |
| 6 | Canvas reapply checks all existing owned companion tracks against the supplied locked-track set before replacing any layers. A conflict leaves the project unchanged. |
| 9–11 | New animation applications save versioned ownership with ordinary editable keys in the clip's extension map. Save/reopen and Undo/Redo preserve that record. Trim and Split rewindow the ownership signature only if the original keys were still owned. All bulk and single-clip desktop animation callbacks use the atomic generated-motion command. |
| 12 | Canvas Motion processes style-only reapplications even when movement keys are unchanged. |
| 13 | Generated corner masks can change radius or be removed at zero. Existing authored masks and later manual mask edits are never claimed as generated styling. |
| 14 | Switching from Canvas Motion to Basic/Physics restores the background/mask values from before Canvas Motion only while those channels still belong to the feature and match its last applied values. Independent user edits remain. This is the concrete proposed style-ownership policy for review. |
| 15 | Physics spends its bounded sample budget on the active interval, increasing density with speed. Canvas single-shot motion samples its entry/exit intervals instead of the whole clip. Generated key vectors bypass the manual key hit tolerance so distinct nearby visible-frame samples survive application and reopen. |
| 16 | Start/End X/Y UI bounds match backend validation: −0.45 through +0.45. |

The common replace/update rule is implemented for generated animations in this batch. An explicit command checks the reviewed current key vector and persisted ownership before changing anything; stale plans and unowned manual keys fail without partially applying a batch. Reapplication keeps one generated animation, rather than stacking keys.

### Compatibility and policy limits

- Existing project documents remain readable. New ownership is stored under `generatedMotionV1` in the existing extensible clip map; playback still uses normal native keys.
- Old projects without reliable ownership metadata retain their keys. Ambiguous legacy animations are protected rather than automatically classified as generated. This branch does not reconstruct ownership from arbitrary old key vectors.
- Ownership of movement and each styling channel is separate. A pre-existing authored mask is not overwritten on a second or later Canvas Motion application.
- This does not implement fixed-aperture content cropping or change CUDA support limits. Retaining a speed curve in current backdrop state does not make speed curves supported by the GPU worker.

## Remaining implementation

Items 1–4, 7–8 and 17–19 still require their remaining implementation, integration and validation: Adjustment navigation, explicit Canvas mask/custom-layer confirmation, linked Canvas deletion/trim/split, Canvas and motion composition, animation Favorites/UI, fixed-frame content zoom/crop, Canvas Favorites Randomly, and the remaining shared layout/application rules. Item 4's Canvas companion ownership is separate from the animation ownership fixed under Item 11.

No master-issue item is closed or marked runtime-certified by this batch. Remaining requirements and original item numbering are preserved.

## Validation

- `cargo test --locked -p concat-project --features schema --lib`: **250 passed** using Rust 1.93.1. Includes reopen, cross-family replacement, Undo/Redo, Trim/Split, authored edits, style-only changes, corners to zero, stale-batch rollback, dense-frame sampling, backdrop timing and locked companions.
- `CONCAT_CANVAS_FIXTURE_DIR=/tmp/concat109-canvas-fixtures` was set while running the native tests. The Canvas/worker contract suite then passed **4 tests**, including native dense-key serialization and per-frame scale/X/Y/opacity agreement with the CPU worker planner.
- Existing animation/caption and shared-layout source-contract suites: **16 passed**.
- Modified Rust modules parsed with rustfmt; `git diff --check` passed.
- Full `cargo check -p concat --lib --locked` was attempted but blocked by the environment's missing `fontconfig.pc` / Fontconfig development dependency before desktop typechecking completed. This is not a successful native application check.
- Windows runtime/scaling inspection and physical NVIDIA/CUDA preview/export parity remain unverified. No EXE/image release, merge into main or deployment is included.
