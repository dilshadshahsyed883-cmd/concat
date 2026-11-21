# Issue #94 — Canvas Presets implementation and compatibility plan

Status: staged UI separation on `issue-94-canvas-presets`; remaining work not yet implemented or runtime-verified. This plan is subordinate to [issue #94](https://github.com/dilshadshahsyed883-cmd/concat/issues/94).

## Scope and design invariants

- One source image or video per Canvas Preset. An enlarged/blurred copy of **that same source** is permitted.
- Six unique composition families only: Adaptive Blur, Gallery Mat, Floating Card, Photo Print, Cinema Frame, Editorial Panel.
- **No** Split Screen, Picture-in-Picture, Three-Panel Story, Collage Grid or other multiple-independent-source layouts.
- Color, border color/thickness, positional variants and direction are editable *settings*, never additional catalogue identities.
- Do not generate headline/caption bars. Real text captions are controlled independently.
- Preserve source media and user-authored content, existing selection semantics, lock/hidden-track protection and one-batch Undo/Redo per Apply.
- Do not merge, deploy or claim preview/GPU parity without verification.

## Verified source baseline (2026-11-21)

| Location | Current implementation |
| --- | --- |
| `upstream/src/crates/concat-project/src/canvas_preset.rs` | `Config::preset` only accepts IDs 1–3; `validate` rejects other IDs and versions except v1. Native companion layers are composed via ordinary clips/shapes/masks. |
| `upstream/src/crates/concat/src/lib.rs` | `CanvasPresets.choose` returns serialized `Config::preset(id)`; `apply` passes JSON and existing Images/Videos scope to studio. |
| `upstream/src/crates/concat/src/studio.rs` | Apply parses/validates JSON, then limits targets to selected/all eligible unlocked image/video clips. |
| `upstream/src/crates/concat/ui/workspace/canvas-presets.slint` | Original interface offered three buttons, directly exposed JSON and Apply Selected/All. |
| `upstream/src/crates/concat/ui/workspace/media-pane.slint` | Original Canvas panel also contained Canvas Presets. |
| `cloud_render/concat/tests/test_canvas_presets_contract.py` | Current GPU/manifest fixture tests expect three legacy IDs; cross-language fixtures are opt-in via `CONCAT_CANVAS_FIXTURE_DIR`. |

Native v1 controls already supported: normalized foreground rectangle, border color and width (0–0.1), radius, background mode `source|blur|color|none`, blur (0–1), background color/tint and tint opacity. Existing presets: ID 1 Sports Frame, ID 2 Orange Frame, ID 3 Blurred Fill. The current renderer uses **contain fit** when calculating the foreground within the requested rectangle; it does not expose true user-selectable crop/fill for preset application. Shadow, preset rotation, free-form fit/crop, and a configurable cinematic aspect ratio are **not supported by this configuration** yet. Do not present these as working controls.

## Stable compatibility plan

| Family/style | Stable ID | Target behavior | Compatibility |
| --- | --- | --- | --- |
| Sports Frame (legacy style) | 1 | Exact existing output | Retain for saved projects and user selection; not counted as separate color-only family |
| Orange Frame (legacy style) | 2 | Exact existing output | Retain for saved projects and user selection; not counted as separate color-only family |
| Adaptive Blur / Blurred Fill | 3 | Foreground source with same-source blurred fill | Keep legacy ID/name alias and appearance; this is the Adaptive Blur family |
| Gallery Mat | proposed 4 | Balanced foreground mat with configurable border/margins | New native profile, not implemented |
| Floating Card | proposed 5 | Inset image, rounded mask and adjustable shadow | Shadow needs renderer-companion design before claiming support |
| Photo Print | proposed 6 | Uneven mat with thicker bottom margin, optional tilt | Tilt needs native config + clipping/safe-bounds design |
| Cinema Frame | proposed 7 | Adjustable cinematic crop/letterbox ratio | New aspect-ratio/crop behavior required; cannot infer correct bars solely from contain-fit |
| Editorial Panel | proposed 8 | Image beside empty editorial negative space | No auto-text/caption; use position as variant |

The proposed IDs 4–8 are **reserved in the plan, not active runtime identifiers**. Do not reuse or renumber IDs 1–3. Version the profile format intentionally when adding fields; retain deserialization/reapply of legacy JSON without data loss.

## Implementation stages

1. **Navigation separation (started):** Add a fourth child page under Animations called Canvas Presets; keep Canvas responsible only for fill/background. Preserve independent image/video targeting across page changes; collapse Advanced JSON. This does not change renderer/project schema.
2. **Catalogue and persistent favorites:** Introduce immutable stable IDs, real visually distinguishable thumbnail cards, selection with no side effects, and separate All/Favorites browsing. Favorites persist through the existing application preferences architecture (#91); do not borrow index positions from the 27 animation favorites.
3. **Typed style editor and right inspector:** Route the selected preset to the right-side inspector without editing a clip on selection. Build typed native edit operations rather than browser-side JSON string replacement. Expose only genuinely supported fields, including border width 0 = off, border/background/tint colors, normalized margins, opacity/blur and radius. Retain collapsible Advanced JSON and preserve unknown fields for valid legacy versions. Coordinate numeric stepping with #87 and shared inspector layout with #93.
4. **Add distinct family geometry:** Implement Gallery Mat and Editorial Panel using the existing normalized rectangle/background contract first; add Photo Print only after rotation bounds semantics are defined. Implement Floating Card only with native/GPU shadow parity; Cinema Frame only with explicit crop/fit and aspect-ratio policy. Reconcile Blurred Fill/Adaptive Blur without duplication.
5. **Compatibility and acceptance:** Validate Apply Selected/All, lock/hidden-track skip, undo/redo, save/reopen, manual keyframe conflict behavior, old JSON round-trip, and aspect-ratio range (portrait, landscape, square, 4:1 boundary). Update Rust fixtures and GPU manifest tests without weakening current negative tests; verify preview vs GPU output before release.

## Stage-1 code changes on branch

- `media-pane.slint`: Canvas Presets appears as fourth sub-navigation item; existing preset panel removed from Canvas background page.
- `canvas-presets.slint`: existing media targets are explicit and editable; JSON can be expanded from Advanced rather than filling the main UI.
- Existing `CanvasPresets.choose`, `apply`, `Config::preset` and renderer behavior are intentionally unchanged.

## Verification status

- [x] Read branch source and catalogue/backend validation behavior.
- [x] Retain existing IDs 1–3 and apply callbacks.
- [x] Separate Canvas and Canvas Presets in source; preserve target toggles.
- [ ] Slint build (toolchain not available in this tool session).
- [ ] Desktop visual inspection, Windows scaling and keyboard navigation.
- [ ] Implement persistent favorites/cards and right inspector.
- [ ] Implement/verify remaining distinct families and GPU parity.
