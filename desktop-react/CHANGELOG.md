# React desktop migration candidate — 25 November 2026

Development branch: `feature/react-desktop-ui-20261125`; review: pull request #149. Main and Salad deployments are unchanged.

## Connected implementation

- React, Vite and Ant Design desktop workspace hosted by Windows WebView2; adjustable media and inspector panels, central program monitor and proportional timeline.
- Existing Rust project sessions own edits, undo/redo, autosave, project locks and project serialization. Unknown document fields pass through without a second project writer.
- Native preset generation for 11 Basic, 13 Physics, 35 Canvas motion and 11 canvas compositions. Generated-key ownership protects manual keys; Canvas application retains existing masks.
- Catalogued effects, transitions and audio treatments with parameter inspection, selection/all scope, clear, favorites, random favorites and defaults. Animation favorites and configured defaults use the original native preference store.
- Clip transforms, crop, trim, speed, volume, fades, track visibility/mute/locks and editable Bezier keyframes. Native waveform paths are shown on the timeline.
- Preview stage gestures forward frame-normalized positions to existing move, resize, rotate, mask and cutout-brush controllers. The frontend does not implement a second animation or mask calculation.
- Native audio clock for playback and seeking; composited PNG monitor and optional rendered MP4 playback proxy; local exports with progress and cancellation.
- React panels for captions/karaoke/SRT, microphone recording, speech models, CBuilder, GPU connection/queue and settings. Typed controller forms expose the remaining original service controls without opening the old editor.
- Original keyboard resolution and rebinding controllers receive desktop key events. Project open/close, export and settings shortcuts are handled by the React shell.
- CPU Vulkan runtime packaging uses a checksum-pinned Mesa distribution and a source-pinned Khronos loader. The same native WGSL renderer is retained.
- Windows source-build, real engine integration, WebView2 interaction and packaged-window smoke gates; separate immutable candidate worker-image workflow with no deployment step.

## Verification status

This is a development changelog, not a claim that every item has passed acceptance. See `docs/TEST-REPORT.md` and the exact workflow artifacts. Complex tools currently use a mixture of task-specific panels and typed controller forms; their layout differs from Slint. CUDA hardware execution, real microphone hardware and actual model inference need their own acceptance evidence.
