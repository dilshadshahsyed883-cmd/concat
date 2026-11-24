# Concat React desktop candidate

Development branch: `feature/react-desktop-ui-20261125`. React, Vite and Ant Design run in a Windows WebView2 desktop host. The compiled Concat Rust engine owns projects, undo history, rendering, native media services and credentials. The React bridge holds a single authoritative session and calls the existing controllers on their event-loop thread.

This branch is under implementation and verification. A compiled adapter or visible control alone is not proof of feature parity. Do not replace production installations until the Windows integration and UI acceptance gates pass. No changes are deployed to Salad, and main is not merged.

## Development

Use Python 3.11, Node 22, Rust 1.93 and the pinned native dependencies from `.github/workflows/concat-react-desktop.yml`.

1. Compile `concat` and `concat-ui-presets` from this branch, including their required native DLLs, into `desktop-react/native/`.
2. Install `requirements.txt`, then run `npm ci` and `npm run build` inside `frontend/`.
3. Run `python app.py` from this folder.

`tools/build-windows.ps1` packages the already compiled native build, runs real native bridge integration checks, and creates a portable Windows ZIP. The branch workflow builds the native engine first. The CUDA workflow builds the existing worker Dockerfile and publishes an immutable candidate tag plus digest. It never starts a Salad instance.

`docs/native-service-schema.json` records typed public controller properties and callbacks. Regenerate the adapters with `upstream/src/crates/concat/tools/generate_react_wire.py`; tests reject drift. Settings and provider secrets continue through the original controllers; the Python host stores only React preferences in the stable Concat AppData folder.

## Verification limits

Local JavaScript builds and host/code-generation tests do not prove Windows startup, actual transcription, microphone capture or CUDA rendering. The Windows workflow must compile and exercise the real native bridge before packaging. CUDA hardware execution requires a separate GPU runner. The tools panel currently renders typed controls from the native service schema; it needs additional task-specific layout and acceptance testing before claiming complete UI migration.
