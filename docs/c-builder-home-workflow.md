# C Builder Home workflow

C Builder is a standalone Home screen feature, absent from editor tabs. Home opens cached media selection, narration and editable planning profiles without a dummy project. Generate verifies a native project; Open in Editor opens it explicitly. Returning Home keeps draft settings in builder state.

Narration supplies the default project basename. Manual overrides survive narration changes; Reset restores the current narration name. Project folder, document and timeline names match with collision-safe numbering. Friendly ZIP and GPU MP4 names follow saved project names, while internal job IDs remain stable.

Only unique consumed visual sources are archived under used/. Narration and unused media remain available. Journal recovery, exclusive library locking, hashing, cancellation and rollback protect the transaction. Original paths remain as hard-link compatibility aliases for older projects and are excluded from subsequent builder scans. Cross-device hard-link failures are explicit. Failed archive steps retain a readable generated project and expose Retry. No rendering occurs.

Tests cover project reopen, cached selection, naming/reset/collisions, 13 unique consumed sources, rollback, cancellation, crash recovery, lock contention and symlink rejection. Interactive Windows filesystem/UI and actual CUDA/NVENC validation remain unverified. These workflows produce no EXE, image, release or deployment.

Inventory refresh errors are reported separately from archival success. A transaction needing journal recovery is shown as unconfirmed rather than claiming zero archived files. The generated project remains available, and Retry resolves pending used-media completion.

## Linked Media Selection and compact profile editor (issue #65)

The Home-only Media Selection step uses separate **100% keyword budgets** for
Images and Videos. Enabled positive-share groups consume the budget; disabling a
group rebalances its previous share proportionally across still-enabled groups.
Dragging one keyword adjusts the remaining enabled sliders in a single
transaction. Deterministic largest-remainder rounding (alphabetical tie-break)
keeps integer percentages at exactly 100% without cursor-time drift. An enabled
row at 0% does not contribute any media until the user increases it; an all-zero
selection remains excluded and preview/generation reports no selected media.
Equalize explicitly gives all enabled groups an equal share.

Disabling remembers the group's previous share. On re-enable, it receives its
remembered share **capped at an equal portion** of the newly enabled group count
(the rest is rebalanced proportionally), preventing a formerly 100% keyword from
silently wiping other groups. Users can then drag it up to a larger share.
A single selected group is 100%; no selected positive group cannot generate.
Group percentages describe *relative sampling probabilities*, not exact item
counts. The timeline video-mix curve remains independent of keyword budgets.

Legacy independent weights such as 100/100/100/100 are normalized for display
and on apply/import/preview/save/generate, preserving equal relative selection.
The source JSON imported from disk is not rewritten without an explicit save.
Project profiles carry the same canonical shares through reopen and GPU ZIP
serialization. Cached inventory, narration-derived names, used/ archival and
project generation are not replaced by this UI. Human-friendly profile inputs
control curve endpoints, shot-duration ranges, run limits, cooldown and seed;
the versioned JSON editor is optional under Advanced. Intermediate custom
curve anchors and unexposed advanced fields remain intact during form edits.

Source tests cover normalization, edge cases, persistence and UI routing.
Interactive Windows layout/drag acceptance remains unverified until an EXE
build is specifically authorized.
