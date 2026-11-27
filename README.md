# Concat GPU development

The native Windows editor is based on Concat v0.2.8, pinned to [79116bd](https://github.com/jub0t/Concat/commit/79116bd0f9a103c633d888603ad27890e929dbdc). Its editable source, Cargo lockfile, licence and notices are in `upstream/`. The Windows workflow compiles and smoke-tests the edited Rust/Slint application. Download the development bundle from the run's artifacts.

The legacy desktop GPU Config and GPU Render Queue are retired; the native editor does not initialize the cloud queue or submit GPU jobs directly. Use the web queue for GPU rendering. The editor and GPU worker still share bundle version 1. GPU execution requires a CUDA-capable runner with NVDEC and NVENC.

Windows File menu offers **Prepare ZIP Only (Offline)...** (`Ctrl+Q`), with the save dialog defaulting to `Desktop/Concat`. This saves the current project using the same atomic save path, then asks for a local `.zip` destination and displays creation progress and the final file location. The archive uses the unchanged `Concat-Bundle` worker format, with a private temporary `concat.json` snapshot just like queued GPU preparation. No Salad credentials, connection, queue submission, VPS access, or Drive access is needed. Existing destination files and original media are never overwritten or removed. The prepared ZIP can be uploaded manually elsewhere for later GPU rendering.

The upstream editor retains its AGPL-3.0-or-later licence and third-party notices.

## Source and release map

| Component | Canonical source | Verification |
| --- | --- | --- |
| Windows editor | `upstream/src/` | Rust tests and frozen Windows smoke in `.github/workflows/concat-windows.yml` |
| Project bundle and GPU worker | `cloud_render/concat/` | `.github/workflows/concat-linux.yml` and component tests |
| Concat Web | `concat-web/` | Independent Web essential/security CI; manual VPS deployment only. See [Web README](concat-web/README.md). |
| Desktop GPU client | `concat_gpu/` | Offline packaging and queue-contract checks in the Windows workflow |

Main Windows publishing checks for a matching GPU candidate before creating a commit-specific prerelease. Read that release's acceptance notes: a successful build or container startup is not real-GPU acceptance. Preserve `upstream/LICENSE` and third-party notices when distributing the editor.

## Repository maintenance

See the [repository guide](docs/REPOSITORY_GUIDE.md) for source ownership, validation and release/storage policy, and [CONTRIBUTING.md](CONTRIBUTING.md) for changes and review.

Web-only changes trigger only the Web verification workflows. The existing matched EXE/GPU release-request workflows are unchanged; Web deployment packages only `concat-web/` and retains the existing service and runtime state. The former standalone Web repository is retained for rollback during migration.
