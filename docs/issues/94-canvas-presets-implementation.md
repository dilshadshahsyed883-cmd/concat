# #94 Canvas Presets — implementation and acceptance record

**Scope owner:** [#94](https://github.com/dilshadshahsyed883-cmd/concat/issues/94), integrated with [#93 shared library/inspector](https://github.com/dilshadshahsyed883-cmd/concat/issues/93).

**Status (November 21, 2026):** source implementation prepared alongside #93. Native Slint/Rust, actual Windows EXE, preview and GPU export validation are separate acceptance gates; the existence of a source implementation alone does not certify these runs.

## Approved catalogue and compatibility

| ID | Name | Role |
| --- | --- | --- |
| 1 | Sports Frame | Unchanged legacy preset; preserve saved-project identity |
| 2 | Orange Frame | Unchanged legacy preset; preserve saved-project identity |
| 3 | Adaptive Blur (legacy Blurred Fill) | Same-source blurred background; retain old ID/name compatibility |
| 4 | Gallery Mat | Balanced inset mat with editable framing and border |
| 5 | Floating Card | Rounded inset picture and adjustable shadow settings |
| 6 | Photo Print | Asymmetric photo mat and optional rotation |
| 7 | Cinema Frame | Cinematic letterbox composition |
| 8 | Editorial Panel | Main image framed with unused negative space; no automatic headline |

These are **six distinct single-media layout families**, including Adaptive Blur (ID 3), with IDs 1 and 2 retained only for backwards compatibility. Existing colors, borders, thicknesses and simple position variants are **settings**, not additional families. No Split Screen, Picture-in-Picture, Three-Panel Story, Collage Grid or other independent-media layouts are included.

## Editor architecture

- Under Animations, the sidebar order is **Animations → Transform → Canvas → Canvas Presets**. The **Canvas** sub-tab retains background/transform settings, and **Canvas Presets** shows dedicated library cards.
- Left library: schematic composition thumbnails, name, independently keyboard-accessible Inspect and star controls, All/Favorites filtering and empty state. Browsing and starring never apply to clips.
- Right shared inspector: selected preset name; typed normalized foreground position/size; border width (including 0=off) and color; radius; background selection, blur/tint and color; shadow opacity/offset/color; relevant mat and rotation. Normal edits update a validated native config; Advanced JSON remains collapsed.
- Images and Video Clips target switches remain independent. Apply Selected and Apply All are outside the right inspector's scrolling body, with the original scoped/locked-track semantics.
- Canvas defaults may be saved or reset in app preferences without altering current project clips; persistent favorites use stable `canvas:1` through `canvas:8` keys.
- The engine reuses each original image/video as the only independent media source; any backdrop is generated from the same media. It must never synthesize a headline/caption bar. Existing real captions remain separate.
- Latest `main` animation gallery and Canvas Motion configuration are preserved alongside the shared #93 work; filtering and effects remain separate concerns.

## Verification and known limits

- Native Rust source tests cover preset validation, one-source companion geometry, no generated captions, undo/redo, project save/reopen and all eight IDs. Cross-language GPU document fixtures now cover eight IDs × portrait/landscape = **16** fixtures.
- Source-level UI tests cover eight card names, separate Favorites, typed numeric/color edits, collapsed JSON, dedicated subtab and right Apply footer.
- **Not yet verified:** successful current-head GitHub Actions Rust/Slint build; actual Windows keyboard and 100%, 125%, 150% scaling checks; real GPU preview/export visual parity including shadows, matte, rotation and cinematic presentation.
- A separate user-selectable foreground fit/crop mode or freely adjustable cinematic crop/aspect-ratio control is **not certified** as implemented by this work. The native geometry retains existing source contain behavior where applicable. Do not advertise unsupported crop/fill as working.
- Any failing checks or output differences block issue closure and production release. No deployment is implied by a source commit.
