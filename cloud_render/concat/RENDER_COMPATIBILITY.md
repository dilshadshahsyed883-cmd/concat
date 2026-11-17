# Concat EXE ↔ Salad GPU render compatibility contract

**Owner:** `dilshadshahsyed883-cmd/concat` (desktop packaging and Salad worker).
**VPS_1 role:** authenticated, bounded ZIP relay and persistent queue only; no video-feature gatekeeping.

## Explicit versioned handshake

The Windows Concat GPU client authenticates to the worker and checks
`GET /api/capabilities` for supported worker API/bundle/render modes, plus:

```json
"render_contract": {"id": "concat-cuda-hybrid-v1", "version": 1}
```

`Prepare ZIP Only` / `Send to GPU` write the same render contract into
`bundle.json`. The Concat worker validates any declared render contract
during package restore and rejects unknown or mismatched identifiers/versions.
The offline Python packer writes the same value.

Older saved **v1 ZIPs without `render_contract`** remain recoverable by
the current worker for retry/backward compatibility. This does **not** imply
all their contained editing features are supported by all images.

Update the explicit contract version for incompatible changes, and update
the PowerShell client, Python shared contract, tests and matching worker
image together. Never promote an EXE/image pair without matching contract
tests and Windows/source build and worker startup verification. Actual CUDA
rendering requires an NVIDIA GPU; CI simulation is not GPU acceptance.

## VPS_1 is transport, not renderer

VPS_1 must retain:

- secure Drive link parsing, canonical Drive Bridge, authenticated API/CSRF;
- bound ZIP size, bounded central directory, safe media paths/checksums;
- resumable chunks with SHA-256 and idempotent worker job identity;
- queue persistence/offline retry and verified MP4 download/Drive delivery.

VPS_1 does **not** call Concat `POST /api/validate` before uploading, does
not decide whether PNG/JPEG, transitions, effects or captions are renderable,
and does not invent renderer feature flags. The worker owns final validation
and communicates its real failed/rendered status.

The EXE may call `POST /api/validate` directly for immediate user feedback
before upload. Worker media support must be implemented separately; this
contract does **not** claim existing PNG decoding or GPU ASR capability.
