# Creative transition inventory and source validation

The renderer retains all 20 procedural transition IDs from the existing pack.
This work adds eight distinct original packages and implements the already-existing
Clock Wipe package on CUDA. It does not rename or duplicate the existing ink,
horizontal tear, burn, glitch, liquid, fragmented or flash packages.

| Requested family | Source package / renderer |
| --- | --- |
| Horizontal tear | Existing `concat.torn-paper` |
| Vertical tear | New `concat.paper-rip-vertical` |
| Diagonal jagged tear | New `concat.paper-rip-diagonal` |
| Double-edge tear | New `concat.double-paper-tear` |
| Crumpled unfold | New `concat.paper-unfold` |
| Brush stroke | New `concat.brush-stroke` |
| Page curl / paper flip | New `concat.page-curl` (procedural curl-edge reveal) |
| Center split / double door | New `concat.door-reveal` |
| Diagonal wipe | New `concat.diagonal-wipe` |
| Clock sweep | Existing `concat.clock-wipe`, CUDA implementation added |
| Radial wipe | Existing `concat.pulse-wipe` |
| Ink | Existing `concat.ink-drying` |
| Burn / flash | Existing `concat.burning-flame`, `concat.heat-flash`, `concat.shake-flash` |
| Glitch | Existing `concat.explosion-glitch`, `concat.ghost-radio` |
| Liquid | Existing `concat.liquid-melt` |
| Fragmented reveal | Existing `concat.shards-scatter` |
| Directional linear wipe | Existing `concat.linear-wipe` desktop package; legacy CUDA `wipe` supports directions. Generic per-cut direction/softness serialization remains unsupported. |

The new packages are ordinary catalogue transitions. Existing favorites, Selected,
All, random, favorite-random, duration, per-cut intensity, and Undo/Redo paths
therefore apply without a parallel UI or alternate transition data model.
Intensity controls the new masks' softness and tactile variation. Their procedural
noise is stable across frames, save/reopen and renders. No texture files are used.

CUDA and WGSL execute the same original formulas for the eight new packages.
The scalar `mask_at` function is a deterministic test oracle for mask fixtures;
it is not called while rendering video and is not a CPU video fallback.
Source endpoints return the outgoing/incoming image exactly and do not allocate.
Intermediate blends preserve straight alpha without modifying either source.

## Verification and limits

Python fixture tests cover all nine GPU IDs, preservation of the original 20,
manifest preflight, exact 0/100% masks, finite deterministic intermediate masks,
and distinguishable paper/non-paper patterns. CUDA tests are provided and skip
explicitly if no working CuPy/NVIDIA runtime is available.

Actual GPU compilation, Windows interactive preview, GPU/native visual comparison,
and live 1080p30 NVENC output have not been verified in this environment. No EXE,
Docker image, installer, release, publishing or deployment was performed.

Transition settings now persist in an optional parameter map, keeping old documents compatible. The eight new packages expose validated direction, reverse, speed, easing, seed, softness, roughness, fiber, shadow and paper RGB controls through the transition inspector. Export, native preview and CUDA receive the same values. Legacy transitions retain their supported controls.

The page-curl package is a procedural curved-edge reveal, not a simulated 3D paper mesh. GPU/native visual parity still requires hardware verification; source fixtures do not establish measured GPU rendering success.
