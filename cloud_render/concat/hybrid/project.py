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
            warn(f"Skipped GPU effect {ident} on {clip.get('id', '?')} ({exc})")
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
    if not isinstance(document, dict) or document.get("version", 1) != 1:
        raise ValueError("Unsupported native Concat project version")
    timelines = document.get("timelines") or [document]
    timeline = next((item for item in timelines if item.get("id") == document.get("activeTimelineId")),
                    timelines[0])
    video = timeline.get("video") or document.get("video") or {}
    if (video.get("width"), video.get("height"), video.get("rateNum"), video.get("rateDen"),
            video.get("colorSpace", "sdr")) != (WIDTH, HEIGHT, FPS, 1, "sdr"):
        raise ValueError("The CUDA candidate requires a 1920×1080, 30 fps, SDR timeline")
    if (timeline.get("background") or {}).get("kind", "none") != "none":
        warn("Skipped unsupported GPU timeline background")
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
            warn(f"Skipped sub-frame clip {ident}")
            continue
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
                warn(f"Skipped unsupported audio filters or volume keys on {ident}")
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
                warn(f"Text clip {ident} uses the CUDA caption layout; custom font and positioning are skipped")
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
            warn(f"Skipped unsupported GPU clip kind {kind} on {ident}")
            continue
        for field in ("crop", "mask", "cutout", "stabilize", "enhanced", "background", "speedCurve"):
            if clip.get(field):
                warn(f"Skipped unsupported GPU effect {field} on {ident}")
        if clip.get("blend", "normal") not in ("", "normal"):
            warn(f"Skipped unsupported GPU blend mode on {ident}")
        if any(clip.get(key, 1) != 1 for key in ("stretchX", "stretchY")):
            warn(f"Skipped unsupported GPU stretch on {ident}")
        defaults = Transform()
        constants = {name: number(clip.get(key, getattr(defaults, name)), key) for key, name in KEYS.items()}
        keys = {}
        for key in clip.get("keys") or ():
            name = KEYS.get(key.get("property"))
            if name is None:
                warn(f"Skipped unsupported GPU key {key.get('property')} on {ident}")
                continue
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
                warn(f"Skipped unsupported or unaligned GPU transition {spec.get('id')} on {incoming.id}")
                continue
            incoming.transition = Transition(mode[0], FrameSpan(span.start, span.start+length), mode[1])
            incoming.outgoing = previous
        sorted_lanes.append(tuple(lane))
    for ident, spec in pending.items():
        warn(f"Skipped transition {spec.get('id')} without an adjacent clip on {ident}")
    if not any(clip.path for lane in sorted_lanes for clip in lane):
        raise ValueError("The active timeline contains no visible video or images")
    return Project(tuple(sorted_lanes), tuple(captions), tuple(sounds), end_frame)
