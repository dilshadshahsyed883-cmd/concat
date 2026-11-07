"""Read the native Concat manifest without modifying it or its media."""
from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

from .animations import Transform
from .captions import CaptionStyle, Cue
from .contract import FrameSpan
from .filters import FilterSettings
from .transitions import Transition

FPS = 30
WIDTH, HEIGHT = 1920, 1080
KEYS = {"scale": "scale", "offsetX": "pan_x", "offsetY": "pan_y",
        "rotation": "rotation_degrees", "opacity": "opacity"}
EASE = {"in": (.42, 0, 1, 1), "out": (0, 0, .58, 1), "inOut": (.42, 0, .58, 1)}
TRANSITIONS = {"cross-fade": ("dissolve", "left"), "fade-black": ("fade", "left"),
               "push": ("slide", "left"), "zoom": ("zoom", "left"),
               "wipe-left": ("wipe", "left"), "wipe-right": ("wipe", "right")}



def validate_gpu_manifest(document):
    """GPU worker-owned compatibility check; runs before large media transfer.

    Keep this policy alongside load_project, the actual CUDA renderer adapter.
    No source media is loaded, modified, or uploaded during this check.
    """
    if not isinstance(document, dict) or document.get("version", 1) != 1:
        raise ValueError("Unsupported Concat project version")
    timelines = document.get("timelines") or [document]
    if not isinstance(timelines, list) or not timelines or not all(
            isinstance(item, dict) for item in timelines):
        raise ValueError("Concat project has no valid timeline")
    timeline = next((item for item in timelines
                     if item.get("id") == document.get("activeTimelineId")), timelines[0])
    video = timeline.get("video") or document.get("video") or {}
    if not isinstance(video, dict) or (
            video.get("width"), video.get("height"), video.get("rateNum"),
            video.get("rateDen"), video.get("colorSpace", "sdr")) != (
            WIDTH, HEIGHT, FPS, 1, "sdr"):
        raise ValueError("GPU supports only 1920x1080 30fps SDR timelines")
    if (timeline.get("background") or {}).get("kind", "none") != "none":
        raise ValueError("Unsupported GPU timeline background")
    tracks, clips, media = (timeline.get("tracks"), timeline.get("clips"), document.get("media"))
    if not all(isinstance(value, list) for value in (tracks, clips, media)):
        raise ValueError("Invalid tracks, clips, or media table in Concat project")
    track_ids = {track.get("id") for track in tracks if isinstance(track, dict)}
    media_items = {item.get("id"): item for item in media
                   if isinstance(item, dict) and isinstance(item.get("id"), str)}
    supported_effects = {"concat.contrast", "concat.saturation",
                         "concat.sharpen", "concat.box-blur"}
    unsupported_fields = ("crop", "mask", "cutout", "stabilize", "enhanced",
                          "background", "speedCurve", "shape", "transitionOut")
    if not clips:
        raise ValueError("Active timeline has no clips to render")
    for index, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise ValueError(f"Invalid timeline clip at position {index + 1}")
        ident = clip.get("id") or f"clip {index + 1}"
        kind = clip.get("kind")
        if kind not in ("image", "video", "audio", "text", "layer"):
            raise ValueError(f"Unsupported GPU clip kind {kind} on {ident}")
        if clip.get("trackId") not in track_ids:
            raise ValueError(f"Missing track for GPU clip {ident}")
        if kind in ("image", "video", "audio"):
            item = media_items.get(clip.get("mediaId"))
            if not item or item.get("placeholder"):
                raise ValueError(f"Missing or placeholder media for GPU clip {ident}")
            if kind == "image" and Path(str(item.get("path") or "")).suffix.lower() not in (
                    ".jpg", ".jpeg"):
                raise ValueError(f"GPU clip {ident} requires a JPEG photo; PNG is not supported")
            if kind == "image" and clip.get("sourceStart", 0):
                raise ValueError(f"GPU clip {ident} cannot trim a still image")
        for field in unsupported_fields:
            if clip.get(field):
                raise ValueError(f"Unsupported GPU feature {field} on clip {ident}")
        if clip.get("blend", "normal") not in ("", "normal"):
            raise ValueError(f"Unsupported GPU blend mode on clip {ident}")
        if any(clip.get(field, 1) != 1 for field in ("stretchX", "stretchY")):
            raise ValueError(f"Unsupported GPU stretch on clip {ident}")
        if clip.get("filters"):
            raise ValueError(f"Unsupported GPU audio/legacy filters on clip {ident}")
        keys = clip.get("keys") or []
        if not isinstance(keys, list):
            raise ValueError(f"Invalid GPU keyframes on clip {ident}")
        for key in keys:
            prop = key.get("property") if isinstance(key, dict) else None
            if kind not in ("image", "video", "layer") or prop not in KEYS:
                raise ValueError(f"Unsupported GPU keyframe {prop} on clip {ident}")
        effects = clip.get("videoEffects") or []
        if not isinstance(effects, list):
            raise ValueError(f"Invalid GPU effects on clip {ident}")
        for effect in effects:
            if not isinstance(effect, dict):
                raise ValueError(f"Invalid GPU effect on clip {ident}")
            if effect.get("enabled", True) is False:
                continue
            effect_id = effect.get("id")
            if kind not in ("image", "video", "layer") or effect_id not in supported_effects:
                raise ValueError(f"Unsupported GPU effect {effect_id} on clip {ident}")
            if effect.get("keys"):
                raise ValueError(f"Unsupported GPU animated effect {effect_id} on clip {ident}")
        transition = clip.get("transitionIn")
        if transition:
            if kind not in ("image", "video") or not isinstance(transition, dict) or (
                    transition.get("id") not in TRANSITIONS):
                raise ValueError(f"Unsupported GPU transition on clip {ident}")
        if kind == "text":
            style = clip.get("text") or {}
            allowed = {"content", "fontSize", "color", "strokeColor",
                       "background", "strokeWidth"}
            if not isinstance(style, dict) or set(style) - allowed:
                raise ValueError(f"Unsupported custom caption styling on clip {ident}")
            if any(clip.get(field) for field in ("keys", "transitionIn", "flipH", "flipV")):
                raise ValueError(f"Unsupported caption animation on clip {ident}")
        if kind == "audio" and clip.get("transitionIn"):
            raise ValueError(f"Unsupported audio transition on clip {ident}")
    return timeline

def number(value, name):
    if type(value) not in (float, int) or not isfinite(value):
        raise ValueError(f"Invalid {name} in Concat project")
    return float(value)


def positive_time(value, name):
    result = number(value, name)
    if not 0 <= result <= 14400:
        raise ValueError(f"{name} is outside the supported four-hour range")
    return result


def ease(fraction, curve):
    if isinstance(curve, str):
        curve = EASE.get(curve)
    if not isinstance(curve, (tuple, list)) or len(curve) != 4:
        return fraction
    x1, y1, x2, y2 = (number(point, "keyframe easing") for point in curve)

    def axis(t, p1, p2):
        inv = 1 - t
        return 3 * inv * inv * t * p1 + 3 * inv * t * t * p2 + t * t * t

    lo, hi = 0.0, 1.0
    for _ in range(18):
        t = (lo + hi) / 2
        if axis(t, min(max(x1, 0), 1), min(max(x2, 0), 1)) < fraction:
            lo = t
        else:
            hi = t
    return axis((lo + hi) / 2, y1, y2)


@dataclass
class Picture:
    id: str
    kind: str
    path: Path | None
    span: FrameSpan
    source_start: float
    speed: float
    constants: dict
    keys: dict = field(default_factory=dict)
    filters: tuple[FilterSettings, ...] = ()
    transition: Transition | None = None
    outgoing: Picture | None = None
    flip_h: bool = False
    flip_v: bool = False

    def transform(self, index):
        fraction = min(max((index - self.span.start) / (self.span.end - self.span.start), 0), 1)
        values = self.constants.copy()
        for name, points in self.keys.items():
            right = bisect_right(points, fraction, key=lambda point: point[0])
            if right in (0, len(points)):
                values[name] = points[0 if right == 0 else -1][1]
                continue
            left, after = points[right - 1], points[right]
            weight = ease((fraction - left[0]) / (after[0] - left[0]), after[2])
            values[name] = left[1] + (after[1] - left[1]) * weight
        values["opacity"] = min(max(values["opacity"], 0), 1)
        values["scale"] = min(max(values["scale"], .0001), 1e4)
        return Transform(**values)


@dataclass(frozen=True)
class Sound:
    path: Path
    start: int
    source_start: float
    seconds: float
    speed: float
    stream: int | None
    volume: float
    fade_in: float
    fade_out: float


@dataclass(frozen=True)
class Project:
    lanes: tuple[tuple[Picture, ...], ...]
    captions: tuple[tuple[Cue, CaptionStyle], ...]
    sounds: tuple[Sound, ...]
    frames: int


def filters_for(clip, warn):
    filters = []
    for effect in clip.get("videoEffects") or ():
        if effect.get("enabled", True) is False:
            continue
        ident, params = effect.get("id"), effect.get("params") or {}
        try:
            if effect.get("keys"):
                raise ValueError("animated parameters")
            if ident in ("concat.contrast", "concat.saturation"):
                name = ident.split(".")[1]
                adjustment = number(params.get("amount", 0), name)
                settings = FilterSettings(**{name: 1 + adjustment / 100})
            elif ident == "concat.sharpen":
                settings = FilterSettings(sharpen=number(params.get("amount", 1), "sharpen"))
            elif ident == "concat.box-blur":
                settings = FilterSettings(blur_radius=round(number(params.get("radius", 4), "radius")))
            else:
                raise ValueError("unsupported effect")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unsupported GPU effect {ident} on {clip.get('id', '?')} ({exc})") from exc
        else:
            filters.append(settings)
    return tuple(filters)


def safe_media(root, item):
    if item.get("placeholder"):
        raise ValueError("A timeline clip references an unfilled media placeholder")
    path = (root / item["path"]).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("A timeline media path escapes the project bundle")
    return path


def color(value, fallback):
    if isinstance(value, str) and value.startswith("#"):
        try:
            raw = bytes.fromhex(value[1:])
            if len(raw) in (3, 4):
                return tuple(raw) if len(raw) == 4 else (*raw, 255)
        except ValueError:
            pass
    return fallback


def load_project(root: Path, warn) -> Project:
    root = root.resolve(strict=True)
    manifest = next((root / name for name in ("concat.json", "wolfcut.json")
                     if (root / name).is_file()), None)
    if manifest is None:
        raise ValueError("Missing native Concat project")
    document = json.loads(manifest.read_text(encoding="utf-8-sig"))
    timeline = validate_gpu_manifest(document)
    tracks = {track["id"]: track for track in timeline["tracks"]}
    media = {item["id"]: item for item in document["media"]}
    lanes = {track: [] for track in tracks}
    sounds, captions, pending = [], [], {}
    end_frame = 0
    for clip in timeline["clips"]:
        ident, kind = clip["id"], clip["kind"]
        track = tracks[clip["trackId"]]
        start = positive_time(clip["start"], "clip start")
        duration = positive_time(clip["duration"], "clip duration")
        first, end = round(start * FPS), round((start + duration) * FPS)
        if end <= first:
            raise ValueError(f"GPU clip {ident} has a sub-frame duration")
        end_frame = max(end_frame, end)
        span = FrameSpan(first, end)
        visible = kind != "audio" and track.get("visible", True)
        item = media.get(clip.get("mediaId"), {})
        audible = (kind == "audio" or kind == "video" and item.get("hasAudio"))
        audible = audible and not (track.get("muted") or clip.get("muted"))
        if not visible and not audible:
            continue
        speed = number(clip.get("speed", 1), "speed")
        if not .01 <= speed <= 100:
            raise ValueError(f"Clip {ident} has unsupported playback speed")
        source = positive_time(clip.get("sourceStart", 0), "source start")
        if audible:
            if clip.get("filters") or any(k.get("property") == "volume" for k in clip.get("keys", ())):
                raise ValueError(f"Unsupported audio filters or volume keys on {ident}")
            sounds.append(Sound(safe_media(root, item), first, source, (end-first)/FPS, speed,
                                clip.get("audioStream"), number(clip.get("volume", 1), "volume"),
                                positive_time(clip.get("fadeIn", 0), "fade in"),
                                positive_time(clip.get("fadeOut", 0), "fade out")))
        if not visible:
            continue
        if kind == "text":
            style = clip.get("text") or {}
            content = style.get("content", "").strip()
            if content:
                cue = Cue(len(captions)+1, round(first*1000/FPS), round(end*1000/FPS), content)
                caption_style = CaptionStyle(
                    font_size=min(512, max(8, round(number(style.get("fontSize", .045), "font size") * HEIGHT))),
                    fill=color(style.get("color"), (255, 255, 255, 255)),
                    outline=color(style.get("strokeColor"), (0, 0, 0, 255)),
                    background=color(style.get("background"), (0, 0, 0, 0)),
                    outline_width=min(32, max(0, round(number(style.get("strokeWidth", 0), "stroke") * HEIGHT))),
                )
                captions.append((cue, caption_style))
            continue
        if kind not in ("image", "video", "layer"):
            raise ValueError(f"Unsupported GPU clip kind {kind} on {ident}")
        for field in ("crop", "mask", "cutout", "stabilize", "enhanced", "background", "speedCurve"):
            if clip.get(field):
                raise ValueError(f"Unsupported GPU effect {field} on {ident}")
        if clip.get("blend", "normal") not in ("", "normal"):
            raise ValueError(f"Unsupported GPU blend mode on {ident}")
        if any(clip.get(key, 1) != 1 for key in ("stretchX", "stretchY")):
            raise ValueError(f"Unsupported GPU stretch on {ident}")
        defaults = Transform()
        constants = {name: number(clip.get(key, getattr(defaults, name)), key) for key, name in KEYS.items()}
        keys = {}
        for key in clip.get("keys") or ():
            name = KEYS.get(key.get("property"))
            if name is None:
                raise ValueError(f"Unsupported GPU key {key.get('property')} on {ident}")
            at = number(key["at"], "key position")
            if not 0 <= at <= 1:
                raise ValueError(f"Clip {ident} has a key outside its duration")
            keys.setdefault(name, {})[at] = (at, number(key["value"], "key value"), key.get("ease"))
        picture = Picture(ident, kind, None if kind == "layer" else safe_media(root, item),
                          span, source, speed, constants,
                          {key: sorted(points.values()) for key, points in keys.items()},
                          filters_for(clip, warn), flip_h=bool(clip.get("flipH")),
                          flip_v=bool(clip.get("flipV")))
        lanes[clip["trackId"]].append(picture)
        if clip.get("transitionIn"):
            pending[ident] = clip["transitionIn"]

    sorted_lanes = []
    for lane in reversed(tuple(lanes.values())):
        lane.sort(key=lambda clip: clip.span.start)
        for previous, incoming in zip(lane, lane[1:]):
            spec = pending.pop(incoming.id, None)
            if not spec:
                continue
            mode = TRANSITIONS.get(spec.get("id"))
            span = incoming.span
            length = min(round(positive_time(spec.get("duration", 1), "transition") * FPS),
                         span.end-span.start)
            if not mode or previous.span.end != span.start or length < 2 or "layer" in (incoming.kind, previous.kind):
                raise ValueError(f"Unsupported or unaligned GPU transition {spec.get('id')} on {incoming.id}")
            incoming.transition = Transition(mode[0], FrameSpan(span.start, span.start+length), mode[1])
            incoming.outgoing = previous
        sorted_lanes.append(tuple(lane))
    for ident, spec in pending.items():
        raise ValueError(f"GPU transition {spec.get('id')} lacks an adjacent clip on {ident}")
    if not any(clip.path for lane in sorted_lanes for clip in lane):
        raise ValueError("The active timeline contains no visible video or images")
    return Project(tuple(sorted_lanes), tuple(captions), tuple(sounds), end_frame)
