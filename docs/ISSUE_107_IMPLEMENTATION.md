# Issue 107 implementation

User authorized implementation, main merge and matching image/EXE builds on 22 November 2026. This supersedes the original documentation-only scope of the seven logged items.

| Item | Result |
| --- | --- |
| 1 | Canvas Presets inspector contains preset targets/settings and one pinned Apply pair. Independent background editing remains under Canvas. |
| 2 | Transform uses consolidated Apply Selected/All; immediate Fit/Fill behavior is retained through a Framing menu, including live edit and keyframe protection. |
| 3 | Basic (including Physics), Canvas and Favorites are direct tabs. Both generators share the exact-key ownership guard; generated keys replace across families, while manual/ambiguous reopened keys remain protected. |
| 4 | One catalogue card grid, search and persistent stars serve Color, Particles and Favorites. Visual card clicks inspect; explicit plus/Apply modifies clips. Particle actions retain their own target/mode dispatch and update-same-ID/stack-distinct-ID behavior. Existing package artwork is used; missing artwork retains the honest fallback. |
| 5 | Animation media targets and bulk scope appear once above right-side settings. Random labels explicitly identify target scope and family pool. Selection name/family stays visible when browsing another category. |
| 6 | Transition random actions are in the right inspector with and without a selected cut. Advanced Transition Controls starts collapsed and preserves its shared JSON buffer. Default buttons and redundant footer are removed; stored defaults/callbacks remain compatible. |
| 7 | Caption cards remain preview-only, with Font/Colors and nonduplicated pinned Apply actions on the right. Redundant copy and the second style editor while a preset is staged are removed. Caption generation/All semantics are retained. |

## Verification

- Project library: 238 tests pass, including the new cross-family replacement/manual protection regression for images and video, historical signatures and ambiguous reloads.
- Animation/caption, shared-layout and caption-preset UI source contract suites: 24 checks.
- Complete Slint tree compiled with the existing cached source-check compiler; standard CI also recompiles the tree.
- Native Rust typechecking, release workflows and artifact provenance are recorded on the PR/Actions runs, rather than assumed from source assertions.

Manual Windows 100/125/150% visual inspection and a live NVIDIA GPU render remain unverified. Image startup smoke is separate from real GPU render acceptance. No live Salad/VPS deployment is included.

Generated-key ownership is session-based: saved projects preserve ordinary keys, but on reopening without a matching ownership record, those keys are protected and Apply reports the conflict rather than erasing or stacking them. Issue #97 full-frame particle rendering remains separate.
