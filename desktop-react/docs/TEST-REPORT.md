# Migration verification log

This file distinguishes execution from mocks and from pending checks. It is updated during implementation; workflow reports are authoritative for their exact source SHA.

| Check | Evidence so far | Classification |
|---|---|---|
| React production bundle | Production Vite build succeeds | Real build |
| Timeline overlap/gap duration, fractional FPS snapping, clock | 4 Node tests pass | Real unit tests |
| RPC exposure, atomic settings, invalid preference recovery, generated controller schema drift | 5 Python tests pass | Real local tests |
| Lock rejection, failed-save close protection, selection routing, callback results, native preferences routing, unknown fields | 9 Python tests pass against controlled engine responses | Simulated engine tests; not rendering evidence |
| Native project model, motion ownership, compact recipes, canvas layouts | 285 `concat-project` tests pass locally | Real Rust tests |
| Preset adapter | Native helper compiled; all 70 catalogue presets generated valid commands | Real native command generation; not 70 video renders |
| Windows native editor and helper | Compiled successfully at `883a420a6ce09ab46a3030514f218748d513522d`; Windows project/host tests passed | Real Windows compilation/tests |
| PNG preview and MP4 export on that runner | Preview failed because the Vulkan CPU adapter could not load; the failure blocked packaging | Failed integration test; not passed |
| CPU runtime integration | Pinned CPU ICD, app-local loader and independent instance/device discovery gate added; Windows validation pending | Pending |
| Latest native source | Native timeline drop/zoom, persistent selection, stage echo preservation, service initialization and saved field hydration corrected | Acceptance run required |
| WebView2 interactions | Test exercises ten-clip project boot, offscreen selection, Physics application, monitor image and settings | Test written; execution pending |
| Packaged portable EXE | Bundled-engine and React-workspace boot gate included after packaging | Pending |
| Existing CUDA worker image | Earlier candidate builds passed imports, compatibility contract and authenticated container entrypoint | Real container smoke; not GPU hardware rendering |
| CUDA rendering on NVIDIA hardware | No CUDA device exercised by these GitHub-hosted jobs | Unverified |
| Microphone capture / real speech model inference | Controllers retained; no hardware/model acceptance run recorded here | Unverified |

The build fails on unexpected integration errors. It does not relabel a missing renderer as a successful render or package a failed acceptance run as a finished release. Main has not been merged and no Salad group has been changed.
