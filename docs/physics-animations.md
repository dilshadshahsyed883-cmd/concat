# Physics Motion — Concat Desktop / Salad GPU

## Engineering scope

The feature operates on **image and video clips** in the existing editor, with the
same native keyframe serialisation already accepted by the Salad CUDA hybrid renderer.
There is no duplicate animation format, no timeline migration, and no new render
service. The renderer continues to perform transforms with the existing CUDA
`transform_rgba` inverse-mapping kernel and NVENC output pipeline.

## Six physically motivated motion presets

| UI name | Simulated dynamics | Native property | Shape |
| --- | --- | --- | --- |
| Spring Zoom | Underdamped mass/spring/damper step response (ζ=.45, ω=18) | Scale | Zoom + settle |
| Pendulum Swing | Linearized pendulum with exponential damping | Rotation | Oscillating angular swing |
| Gravity Bounce | Projectile flight under constant g, impact restitution e=.56 | Vertical offset | Fall + decreasing rebounds |
| Elastic Pop | Damped elastic impulse | Scale | Overshoot + recovery |
| Inertia Glide | Exponentially decaying velocity / drag | Horizontal offset | Slide + settle |
| Damped Shake | Superposed decaying oscillation modes | Rotation | Organic impact/shake |

Each trajectory is deterministic. The native project stores **only normal**
`ClipKey` entries. The samples are placed on actual visible source frames, up to
64 segments per clip, using linear interpolation between samples. Duration is
the existing clip duration; clips shorter than two visible frames are rejected.
Presets clamp scale/pan to the existing native property limits and preserve the
user's existing framing.

## Controls

- Select one image or video, then click its physics preset. A single undoable
  command applies the sampled keys. Pre-existing keys on the same property
  are never overwritten.
- Click ⭐ beside a physics preset to add or remove it from personal favorites.
  The stars persist in the app's `settings.json`, not the project.
- **Apply Favorites to All Randomly** uses only starred physics presets,
  applies to eligible, visible and unlocked picture clips, skips authored keys,
  and performs one undoable batch for the entire timeline.
- A second click rerolls the owned/generated keys, choosing different alternatives
  when available. It avoids consecutive identical eligible favorite presets
  where choices allow.
- The previous **Apply All Randomly** remains and adds the six physics
  presets to the original three native motion choices.

## GPU compatibility

The native renderer / desktop preview read the same keyframes as before,
including `scale`, `offsetX`, `offsetY`, and `rotation`. The worker preflight
will reject unsupported transforms as usual. **No unverified distortion/stretch
presets** or desktop-only shaders are exposed. The native export still uses
CUDA for final transforms; creating keyframe values on the CPU is not a
CPU video-rendering fallback.

The current GPU worker contract restricts output to 1080p/30fps SDR and JPEG
image inputs; this feature does not change those restrictions.

## Validation and rollout

- Rust tests cover 6 presets, image/video eligibility, short clips, deterministic
  output, frame-aligned monotonic keys, finite bounded samples, and authored-key
  protection.
- Python tests exercise the existing worker preflight and native key sampling.
- CI should parse Rust/Slint, run worker regressions, and perform a native
  compile when available; a green Python-only check does **not** prove the
  Windows UI compiled.
- Changes stay on `animation/physics-presets-20261108`. Do **not** merge
  to `main` or trigger an EXE/GPU image release until acceptance.

### Future refinement (not implemented)

An optional detailed preset editor may expose damping, stiffness, gravity,
restitution, speed, and per-clip duration/strength. Any such control should
regenerate existing native keys rather than introduce another project format.
