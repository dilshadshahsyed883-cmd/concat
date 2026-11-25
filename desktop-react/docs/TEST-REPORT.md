# Migration verification log

This file distinguishes execution from mocks and from pending checks. It is updated during implementation; workflow reports are authoritative for their exact source SHA.

| Check | Evidence so far | Classification |
|---|---|---|
| React production bundle | Production Vite build succeeds | Real build |
| Timeline overlap/gap duration, fractional FPS snapping, clock | 3 Node tests pass | Real unit tests |
| RPC exposure, atomic settings, generated controller schema drift | 3 Python tests pass | Real local tests |
| Lock rejection, failed-save close protection, selection routing, callback results, native preferences routing, unknown fields | 9 Python tests pass against controlled engine responses | Simulated engine tests; not rendering evidence |
| Native project model, motion ownership, compact recipes, canvas layouts | 285 `concat-project` tests pass locally | Real Rust tests |
| Preset adapter | Native helper compiled; all 70 catalogue presets generated valid commands | Real native command generation; not 70 video renders |
| Windows native editor and helper | Compiled successfully at `fbddc1849221a79f4895d74a6495714f3095dd6e`; Windows project/host tests passed | Real Windows compilation/tests |
| PNG preview and MP4 export on that runner | Preview failed because no graphics/software adapter was available | Failed integration test; not passed |
| CPU runtime integration | Pinned Vulkan CPU adapter and loader added; next Windows gate required | Pending |
| Latest native source | Subsequent boxed lock-wrapper and preset metadata macro errors were corrected; the updated helper compiled and all 70 adapter paths passed locally | Rebuild required |
| WebView2 interactions | Test exercises native project boot, timeline selection, Physics application, monitor image and settings | Test written; execution pending |
| Packaged portable EXE | Window-startup gate included after packaging | Pending |
| Existing CUDA worker image | Earlier candidate builds passed imports, compatibility contract and authenticated container entrypoint | Real container smoke; not GPU hardware rendering |
| CUDA rendering on NVIDIA hardware | No CUDA device exercised by these GitHub-hosted jobs | Unverified |
| Microphone capture / real speech model inference | Controllers retained; no hardware/model acceptance run recorded here | Unverified |

The build fails on unexpected integration errors. It does not relabel a missing renderer as a successful render or package a failed acceptance run as a finished release. Main has not been merged and no Salad group has been changed.
