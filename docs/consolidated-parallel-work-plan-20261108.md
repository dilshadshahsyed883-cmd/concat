# Concat consolidated parallel implementation — 2026-11-08

## Immutable starting point
- Repository: `dilshadshahsyed883-cmd/concat`
- Base commit: `77fd6601b49caece5a91b693fdb64c8b08da0316`
- Integration branch: `work/consolidated-concat-20261108`
- Do **not** merge into `main` during parallel implementation.
- Preserve existing projects, media, metadata, queue, credentials, audio, renders and workspaces.
- No automatic VPS deployment, Salad restart or Auto Script.
- Target remains CUDA/NVENC **1080p30 SDR**. Do not silently change export scope.
- Desktop & GPU worker must share the same versioned project compatibility contract.

## Work branches and acceptance
1. `work/concat-manifest-large-projects-20261108` — Task 1
   - Eliminate 2 MiB manifest validation cap in both desktop PowerShell helper and worker.
   - Enforce resource-safe authenticated JSON validation; test small, large, malformed and stress manifests; never lose resumable transfer state.
2. `work/concat-gpu-performance-20261108` — Task 2
   - Profile decode, seek, CUDA effects, frame copies, sync, NVENC, audio and captions.
   - Optimize only measured bottlenecks with same-workload before/after metrics and quality/synchronization parity checks.
   - No silent CPU video fallback; no fabricated RTX 3060 Ti benchmarks.
3. `work/concat-cbuilder-bulk-targeting-20261108` — Tasks 3–4
   - Produce a shared **visual** track containing generated images + videos in sequence; narration stays on audio track.
   - Implement image/video checkboxes for bulk animation, random favourites, scale and position.
   - Support selected-media and timeline scope, empty-selection errors, manual-keyframe protection, single Undo/Redo and persistence.
4. `work/concat-tabs-animation-captions-20261108` — Tasks 5–7
   - Top library order: Media, C Builder, Text, Transitions, Animations, Filters, Templates, Effects.
   - Remove duplicate top Audio tab, keep Media audio functionality.
   - Move six physics presets & favourite/application controls into Animations shelf; retain per-clip physics parameters in Inspector.
   - Add Generate Captions entry to Text > Captions, opening existing transcription/script/SRT workflow. Preserve precise Whisper word timing and CUDA style parity.
5. `work/concat-transcriber-canvas-blur-20261108` — Tasks 8–9
   - Harden Tiny/Base/Small model downloading: honest byte progress, resume, checksum, cancellation, partial recovery, install detection.
   - Add Canvas Fit / Fill-Crop / Blur Background / Solid Color and 0–100 blur strength, selected/all application.
   - Implement foreground/background compositing and GPU rendering as well as Desktop preview; no UI-only unsupported mode.
6. `work/concat-timeline-compound-20261108` — Tasks 10–12
   - Compact picture lanes to ~45–50px by default with adjustable heights and readable tracks.
   - Timeline must span full window width below preview and Inspector, including resizing.
   - Implement non-destructive compound selected clips / all layers / time range; edit/ungroup/rename/nest with cycle guard, undo/redo, save/open and CUDA render parity.
   - Preserve tracks, relative timing, gaps, audio sync and original media.

## Coordination rules (mandatory before cross-component changes)
- Shared native project shape is owned by the integration contract; **Agent 6** must document any compound schema additions and backward-compatible migration before landing consumer code. Agents 1, 2, 3 and 5 must review affected manifest/render assumptions.
- **Agent 4** owns the library strip and Animations shelf. **Agent 3** owns bulk-target operation logic and selectors; agree callback API before editing overlapping Slint/Rust panels.
- **Agent 2** owns performance changes in CUDA renderer. **Agent 5** owns Canvas rendering feature. Do not concurrently overwrite the same renderer sections: merge Canvas contract first, then replay and re-benchmark performance changes.
- **Agent 6** owns dock/timeline structure. Agents 3 and 4 must avoid uncoordinated dock or timeline layout rewrites.
- **Agent 1** owns manifest-size validation. Other agents must not reintroduce restrictive limits.
- Use separate worktrees, focused commits, tests and PRs targeted **to the integration branch**, not directly to main.
- Do not rebase and force-push somebody else's work. Rebase/reconcile once with current integration branch before PR completion.
- Treat inactive or untouched media as immutable; no migration of user files by a branch.

## Integration and release gates
1. All six branch implementations and their focused tests pass.
2. Merge PRs **into `work/consolidated-concat-20261108`**, resolving shared file conflicts intentionally.
3. Run full Rust+Slint source checks; relevant Windows build and smoke; Python worker tests, manifest large fixtures, captions/transcriber, C Builder, bulk action Undo/Redo, timeline compounds, save/reopen and existing project regression.
4. Check Preview ↔ CUDA export parity with realistic mixed-media, captions, transitions, animation, filters, compounds and Canvas. Report simulated/CI checks separately from hardware acceptance.
5. Inspect final diff, protect user data and existing queue/protocol, check for unsupported project fields and false “success” reporting.
6. Only when integration is green and explicitly ready for final promotion, merge integration branch into `main` without bypassing branch protections.
7. From one identical final `main` SHA produce **one** portable Windows x64 EXE ZIP and **one** Linux Salad GPU container; verify Windows smoke, release ZIP SHA-256, GHCR tag/digest and rollback availability.
8. No VPS/Salad automatic deployment; publish release artifacts without switching live instances.

## Progress accounting
Creating branches or this document is **planning only**. Do not claim code is complete, deployed, CI-verified, or benchmarked until supported by PRs, workflow results and (for speed) hardware measurements.
