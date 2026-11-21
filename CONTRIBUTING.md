# Contributing to Concat desktop and GPU worker

Start with [README.md](README.md) and the [repository guide](docs/REPOSITORY_GUIDE.md). Choose one application or component and create a focused branch from its canonical source branch. Describe the user problem and acceptance criteria before changing behavior.

## Change and review

1. Keep documentation, application behavior and CI changes reviewable as separate concerns. Preserve unrelated apps and existing upstream notices.
2. Run the affected component's existing tests. Record exact commands, results and any untested platform behavior in the pull request.
3. For a release, record the full source SHA, artifact SHA256, dependency/model versions and previous verified version. Do not use a branch name as immutable release evidence.
4. Request review through a pull request. Repository rules and required checks determine merge eligibility; this guide does not claim that particular account settings are enabled.

## Validation entry point

Use the exact targeted commands in .github/workflows/concat-linux.yml and concat-windows.yml. Rust checks run from upstream/src with the checked-in Cargo.lock. CPU/offline tests do not certify CUDA rendering.

## Data and licensing

Use synthetic fixtures or media you are authorized to share. Do not commit credentials, OAuth files, private media, customer data, model weights or runtime databases. Report sensitive issues through GitHub private vulnerability reporting if enabled, or an established private channel with the repository owner; do not put exploit details or secrets in a public issue. Retain component licences and third-party attribution. Do not assign a new blanket licence to vendored or mixed-ownership code.

## Deployment

A successful source or packaging check is evidence for that check only. Production release, model-image replacement and data migration require their existing app-specific validation and rollback procedures. A documentation or housekeeping change does not authorize them.
