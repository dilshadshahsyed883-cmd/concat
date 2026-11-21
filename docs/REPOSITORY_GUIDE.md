# Concat desktop and GPU worker: repository guide

Source review: 21 November 2026. This is a source map, not a claim about live deployment health.

Rust/Slint desktop editor plus project-bundle and GPU-rendering components. A Windows build, image startup check, and real GPU acceptance are distinct results.

## Source map

| Path | Purpose |
| --- | --- |
| [`upstream/`](../upstream/) | Pinned upstream editor, Rust workspace and licence notices |
| [`concat_gpu/`](../concat_gpu/) | Desktop GPU client integration |
| [`cloud_render/concat/`](../cloud_render/concat/) | Bundle, rendering and worker implementation |
| [`.github/workflows/concat-linux.yml`](../.github/workflows/concat-linux.yml) | Linux source and compatibility checks |
| [`.github/workflows/concat-windows.yml`](../.github/workflows/concat-windows.yml) | Windows packaging and matched release publishing |

## Development and validation

Use the exact targeted commands in .github/workflows/concat-linux.yml and concat-windows.yml. Rust checks run from upstream/src with the checked-in Cargo.lock. CPU/offline tests do not certify CUDA rendering.

## Build and storage policy

The target retention policy is 3 days for PR/test packages, 7 days for ordinary development candidates and sanitized failure diagnostics, and versioned GitHub Releases for supported downloads. The workflow's explicit retention-days is authoritative; this document alone does not change expiry. Release evidence consumed by another workflow needs its own retention exception.

Before retiring any artifact, identify its full source SHA and active consumers. Preserve active integration candidates and the current and previous verified release outside short-lived Actions storage. A candidate that needs longer testing must be copied to a named prerelease with checksums before expiry. No automatic artifact, branch, release or package deletion is introduced by this guide.

Heavy Windows/container packaging should use explicit release or manual triggers where source tests can run independently. Existing frozen-binary, media and compatibility tests remain release gates. Do not cancel an in-progress publication or deployment just to save CI minutes.

## Release record

For each release record: component/version, full source SHA, package SHA256 or container digest, relevant dependency/model versions, exact verification results, unresolved platform acceptance, and previous verified version/rollback procedure. Use sanitized screenshots from the actual app when available; no fabricated UI or unverified production claims.

## Maintenance checklist

- Keep README setup instructions and app/version mappings aligned with the source.
- Review dependency updates with the affected tests; do not enable blind automatic merges.
- Keep workflow token permissions scoped to the job that needs them.
- Inspect repository rules, environment protection and available security features before proposing account-setting changes.
- Keep historical research labelled and separate from the current source contract.
- Use the [contribution guide](../CONTRIBUTING.md) and issue/PR templates for review.
