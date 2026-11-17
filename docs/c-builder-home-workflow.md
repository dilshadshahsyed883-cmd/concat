# C Builder Home workflow

C Builder is a standalone Home screen feature, absent from editor tabs. Home opens cached media selection, narration and editable planning profiles without a dummy project. Generate verifies a native project; Open in Editor opens it explicitly. Returning Home keeps draft settings in builder state.

Narration supplies the default project basename. Manual overrides survive narration changes; Reset restores the current narration name. Project folder, document and timeline names match with collision-safe numbering. Friendly ZIP and GPU MP4 names follow saved project names, while internal job IDs remain stable.

Only unique consumed visual sources are archived under used/. Narration and unused media remain available. Journal recovery, exclusive library locking, hashing, cancellation and rollback protect the transaction. Original paths remain as hard-link compatibility aliases for older projects and are excluded from subsequent builder scans. Cross-device hard-link failures are explicit. Failed archive steps retain a readable generated project and expose Retry. No rendering occurs.

Tests cover project reopen, cached selection, naming/reset/collisions, 13 unique consumed sources, rollback, cancellation, crash recovery, lock contention and symlink rejection. Interactive Windows filesystem/UI and actual CUDA/NVENC validation remain unverified. These workflows produce no EXE, image, release or deployment.
