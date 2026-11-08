"""Read the native Concat manifest without modifying it or its media."""
from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from math import floor, isfinite
from pathlib import Path

from .animations import Transform
from .captions import CaptionStyle, Cue, WordTiming
from .contract import FrameSpan
from .filters import FilterSettings
from .looks import FILTER_LOOKS
from .transitions import NATIVE_TRANSITIONS, Transition

FPS = 30
WIDTH, HEIGHT = 1920, 1080
KEYS = {"scale": "scale", "offsetX": "pan_x", "offsetY": "pan_y",
        "rotation": "rotation_degrees", "opacity": "opacity"}
EASE = {"in": (.42, 0, 1, 1), "out": (0, 0, .58, 1), "inOut": (.42, 0, .58, 1)}
EXPOSURE = "concat.exposure"
CONTRAST = "concat.contrast"
NATIVE_COLOR_PARAMS = {EXPOSURE: ("stops", -4, 4), CONTRAST: ("amount", -100, 100)}
TRANSITIONS = {"cross-fade": ("dissolve", "left"), "fade-black": ("fade", "left"),
               "push": ("slide", "left"), "zoom": ("zoom", "left"),
               "wipe-left": ("wipe", "left"), "wipe-right": ("wipe", "right")}


def validate_gpu_manifest(document):
    """Run the render planner without reading media from the desktop's paths."""
    timeline = _validated_timeline(document)
    _plan_project(document, timeline, _media_path)
    return timeline


def _validated_timeline(document):
    from .compounds import validate_compounds
    if not isinstance(document, dict) or document.get("version", 1) != 1:
        raise ValueError("Unsupported Concat project version")
    timelines = document.get("timelines", [document])
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
    background = timeline.get("background") or {}
    if not isinstance(background, dict) or background.get("kind", "none") != "none":
        raise ValueError("Unsupported GPU timeline background")
    tracks, clips, media = (timeline.get("tracks"), timeline.get("clips"), document.get("media"))
    if not all(isinstance(value, list) for value in (tracks, clips, media)):
        raise ValueError("Invalid tracks, clips, or media table in Concat project")
    validate_compounds(timeline)

    def index_by_id(items, label):
        result = {}
        for item in items:
            ident = item.get("id") if isinstance(item, dict) else None
            if not isinstance(ident, str) or not ident or ident in result:
                raise ValueError(f"Invalid or duplicate {label} ID in Concat project")
            result[ident] = item
        return result

    track_ids = index_by_id(tracks, "track")
    media_items = index_by_id(media, "media")
    index_by_id(clips, "clip")
    unsupported_fields = ("crop", "mask", "cutout", "stabilize", "enhanced",
                          "background", "speedCurve", "shape", "transitionOut")
    if not clips:
        raise ValueError("Active timeline has no clips to render")
    for clip in clips:
        ident = clip["id"]
        kind = clip.get("kind")
        if kind not in ("image", "video", "audio", "text", "layer"):
            raise ValueError(f"Unsupported GPU clip kind {kind} on {ident}")
        track_id = clip.get("trackId")
        if not isinstance(track_id, str) or track_id not in track_ids:
            raise ValueError(f"Missing track for GPU clip {ident}")
        if kind in ("image", "video", "audio"):
            media_id = clip.get("mediaId")
            item = media_items.get(media_id) if isinstance(media_id, str) else None
            if not item or item.get("placeholder"):
                raise ValueError(f"Missing or placeholder media for GPU clip {ident}")
            if kind == "image" and _media_path(item).suffix.lower() not in (
                    ".jpg", ".jpeg"):
                raise ValueError(f"GPU clip {ident} requires a JPEG photo; PNG is not supported")
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
            if kind not in ("image", "video", "layer") or not isinstance(prop, str) or prop not in KEYS:
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
            if (kind not in ("image", "video", "layer")
                    or not (effect_id == "concat.box-blur"
                            or (kind != "layer" and effect_id in NATIVE_COLOR_PARAMS)
                            or (kind == "layer" and effect_id in FILTER_LOOKS))):
                raise ValueError(f"Unsupported GPU effect {effect_id} on clip {ident}")
        transition = clip.get("transitionIn")
        if transition is not None:
            transition_id = transition.get("id") if isinstance(transition, dict) else None
            if (kind not in ("image", "video") or not isinstance(transition_id, str)
                    or (transition_id not in TRANSITIONS and transition_id not in NATIVE_TRANSITIONS)
                    or set(transition) - {"id", "duration", "intensity"}):
                raise ValueError(f"Unsupported GPU transition {transition_id} on clip {ident}")
        if kind == "text" and (any(clip.get(field) for field in ("flipH", "flipV"))
                               or clip.get("rotation", 0) != 0 or clip.get("scale", 1) != 1):
            raise ValueError(f"Unsupported caption transform on clip {ident}")
        if kind in ("audio", "video") and clip.get("preservePitch", True) is False:
            raise ValueError(f"GPU requires preserved audio pitch on clip {ident}")
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


@dataclass(frozen=True)
class NativeColorEffect:
    """A native color pass with one constant or animated catalogue parameter."""

    id: str
    value: float
    keys: tuple[tuple[float, float, tuple | str | None], ...]
    start: float
    duration: float

    def at_frame(self, index):
        if not self.keys:
            return self.value
        fraction = (index / FPS - self.start) / self.duration
        right = bisect_right(self.keys, fraction, key=lambda point: point[0])
        if right == 0:
            return self.keys[0][1]
        if right == len(self.keys):
            return self.keys[-1][1]
        left, after = self.keys[right - 1], self.keys[right]
        if fraction == left[0]:
            return left[1]
        weight = ease((fraction - left[0]) / (after[0] - left[0]), after[2])
        _, minimum, maximum = NATIVE_COLOR_PARAMS[self.id]
        return min(maximum, max(minimum, left[1] + (after[1] - left[1]) * weight))


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
    filters: tuple[FilterSettings | NativeColorEffect, ...] = ()
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
class NativeCut:
    rank: int
    transition: Transition
    outgoing: Picture
    incoming: Picture
    hold_outgoing: bool

    def outgoing_frame(self, index):
        return min(index, self.outgoing.span.end - 1) if self.hold_outgoing else index


@dataclass(frozen=True)
class Project:
    lanes: tuple[tuple[Picture, ...], ...]
    captions: tuple[tuple[Cue, CaptionStyle], ...]
    sounds: tuple[Sound, ...]
    frames: int
    native_cuts: tuple[NativeCut, ...] = ()


def _native_cut(rank, previous, incoming, ident, duration, media_duration, intensity=None):
    length = min(duration, (previous.span.end - previous.span.start) / FPS,
                 (incoming.span.end - incoming.span.start) / FPS)
    frames = floor(length * FPS + .5)
    if frames < 2:
        raise ValueError(f"GPU transition {ident} on {incoming.id} needs at least two frames")
    cut = incoming.span.start
    head = length / 2
    if incoming.kind == "video":
        head = min(head, incoming.source_start / incoming.speed)
    if head < .5 / FPS:
        head = 0
    start = floor(cut - head * FPS + .5)
    end = start + frames
    tail = max(0, end - cut)
    previous_frames = previous.span.end - previous.span.start
    available = ((media_duration - previous.source_start) / previous.speed
                 if type(media_duration) in (float, int) and isfinite(media_duration) else 0)
    hold = previous.kind == "video" and (available * FPS - previous_frames - 1) < tail - .5
    return NativeCut(rank, Transition(NATIVE_TRANSITIONS[ident], FrameSpan(start, end),
                                      intensity=intensity),
                     previous, incoming, hold)


def _native_cut_audio(sounds, cut):
    """Extend audio into the same source handles as video without moving the cut."""
    span = cut.transition.span
    seconds = (span.end - span.start) / FPS
    if (incoming := sounds.get(cut.incoming.id)) is not None:
        lead = max(incoming.start - span.start, 0)
        sounds[cut.incoming.id] = replace(
            incoming, start=incoming.start - lead,
            source_start=max(incoming.source_start - lead * incoming.speed / FPS, 0),
            seconds=incoming.seconds + lead / FPS, fade_in=max(incoming.fade_in, seconds))
    if not cut.hold_outgoing and (outgoing := sounds.get(cut.outgoing.id)) is not None:
        tail = max(span.end - cut.outgoing.span.end, 0)
        sounds[cut.outgoing.id] = replace(
            outgoing, seconds=outgoing.seconds + tail / FPS,
            fade_out=max(outgoing.fade_out, seconds))


def _native_color_effect(effect, params, start, duration):
    effect_id = effect["id"]
    parameter, minimum, maximum = NATIVE_COLOR_PARAMS[effect_id]
    if set(params) - {parameter}:
        raise ValueError("unknown effect parameter")
    value = number(params.get(parameter, 0), parameter)
    if not minimum <= value <= maximum:
        raise ValueError(f"{parameter} must be within {minimum} to {maximum}")
    keys = effect.get("keys", {})
    if not isinstance(keys, dict) or set(keys) - {parameter}:
        raise ValueError("invalid effect keyframe parameters")
    points = keys.get(parameter, [])
    if not isinstance(points, list):
        raise ValueError("invalid effect keyframes")
    parsed = {}
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("invalid effect keyframe")
        at = number(point.get("at"), "effect key position")
        key_value = number(point.get("value"), f"effect key {parameter}")
        if not 0 <= at <= 1 or not minimum <= key_value <= maximum or at in parsed:
            raise ValueError("invalid or duplicate effect keyframe")
        curve = point.get("ease")
        if isinstance(curve, list) and len(curve) == 4:
            curve = tuple(number(part, "effect key easing") for part in curve)
            if curve == (0, 0, 1, 1):
                curve = None
        elif curve is not None and curve not in ("linear", *EASE):
            raise ValueError("invalid effect key easing")
        parsed[at] = at, key_value, curve
    return NativeColorEffect(effect_id, value, tuple(sorted(parsed.values())), start, duration)


def filters_for(clip, start, duration):
    filters = []
    for effect in clip.get("videoEffects") or ():
        if effect.get("enabled", True) is False:
            continue
        ident, params = effect.get("id"), effect.get("params", {})
        try:
            if not isinstance(params, dict):
                raise ValueError("invalid effect parameters")
            if ident in NATIVE_COLOR_PARAMS:
                settings = _native_color_effect(effect, params, start, duration)
            elif ident in FILTER_LOOKS:
                if params or effect.get("keys"):
                    raise ValueError("timeline look accepts only the layer intensity slider")
                settings = FILTER_LOOKS[ident]
            else:
                if effect.get("keys"):
                    raise ValueError("animated parameters")
                if set(params) - {"radius"}:
                    raise ValueError("unknown effect parameters")
                settings = FilterSettings(blur_radius=round(number(params.get("radius", 4), "radius")))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unsupported GPU effect {ident} on {clip.get('id', '?')} ({exc})") from exc
        else:
            filters.append(settings)
    return tuple(filters)


def _media_path(item):
    # Preflight sees the desktop's path (including Windows separators).
    name = item.get("path")
    if not isinstance(name, str) or not name.strip() or "\0" in name:
        raise ValueError(f"Missing or invalid GPU path for media {item.get('id', '?')}")
    return Path(name)


def safe_media(root, item):
    path = (root / _media_path(item)).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("A timeline media path escapes the project bundle")
    return path


def _caption_color(value, field, ident):
    if isinstance(value, str) and len(value) in (7, 9) and value.startswith("#"):
        try:
            raw = bytes.fromhex(value[1:])
            if len(raw) in (3, 4):
                return tuple(raw) if len(raw) == 4 else (*raw, 255)
        except ValueError:
            pass
    raise ValueError(f"Unsupported caption styling {field} on clip {ident}")


def _hanken_font():
    packaged = Path(__file__).with_name("HankenGrotesk-SemiBold.ttf")
    return packaged if packaged.is_file() else (
        Path(__file__).resolve().parents[3]
        / "upstream/src/crates/concat-text/fonts/HankenGrotesk-SemiBold.ttf"
    )


def _caption(clip, first, end, index, track_rank):
    ident = clip["id"]
    style = clip.get("text")
    if not isinstance(style, dict) or not isinstance(style.get("content"), str):
        raise ValueError(f"Invalid caption text on clip {ident}")
    # The Rust serializer writes every TextStyle field. Accept the native
    # defaults where they have no visible effect and reject changed styles
    # that the worker cannot faithfully draw.
    neutral = {"italic": False,
               "backgroundPaddingX": .0315, "backgroundPaddingY": .018,
               "lineHeight": 1.2, "tracking": 0, "maxWidth": 0, "maxHeight": 0}
    supported = {"content", "fontFamily", "fontSize", "fontWeight", "color",
                 "opacity", "strokeWidth", "strokeColor", "shadow", "align",
                 "background", "backgroundRadius", "karaoke", "karaokeColor"}
    for name, value in style.items():
        if name not in supported and (name not in neutral or value != neutral[name]):
            raise ValueError(f"Unsupported caption styling {name} on clip {ident}")
    family = style.get("fontFamily", "Hanken Grotesk")
    if (family not in ("Hanken Grotesk", '"Hanken Grotesk"')
            or style.get("fontWeight", 600) != 600):
        raise ValueError(f"Unsupported caption styling fontFamily or fontWeight on clip {ident}; "
                         "use Hanken Grotesk SemiBold")
    if style.get("align", "center") != "center":
        raise ValueError(f"Unsupported caption styling align on clip {ident}")
    karaoke = style.get("karaoke", False)
    if type(karaoke) is not bool:
        raise ValueError(f"Invalid caption karaoke on clip {ident}")
    shadow = style.get("shadow", True)
    if type(shadow) is not bool:
        raise ValueError(f"Invalid caption shadow on clip {ident}")
    size = round(number(style.get("fontSize", .045), f"caption fontSize on {ident}") * HEIGHT)
    stroke = round(number(style.get("strokeWidth", 0), f"caption strokeWidth on {ident}") * HEIGHT)
    fill = _caption_color(style.get("color", "#ffffff"), "color", ident)
    outline = _caption_color(style.get("strokeColor", "#000000"), "strokeColor", ident)
    background = (_caption_color(style["background"], "background", ident)
                  if style.get("background") else (0, 0, 0, 0))
    highlight = (_caption_color(style["karaokeColor"], "karaokeColor", ident)
                 if style.get("karaokeColor") else (255, 225, 60, 255))
    radius = number(style.get("backgroundRadius", .0135),
                    f"caption backgroundRadius on {ident}")
    if radius < 0 or radius > .2:
        raise ValueError(f"Unsupported caption styling backgroundRadius on clip {ident}")
    anchor_x = .5 + number(clip.get("offsetX", 0), f"caption offsetX on {ident}")
    anchor_y = .5 + number(clip.get("offsetY", 0), f"caption offsetY on {ident}")
    opacity = number(style.get("opacity", 1), f"caption opacity on {ident}")
    clip_opacity = number(clip.get("opacity", 1), f"clip opacity on {ident}")
    if not 8 <= size <= 540 or not 0 <= stroke <= 32:
        raise ValueError(f"Unsupported caption size or outline on clip {ident}")
    if not 0 <= opacity <= 1 or not 0 <= clip_opacity <= 1:
        raise ValueError(f"Unsupported caption opacity on clip {ident}")
    if not all(0 <= anchor <= 1 for anchor in (anchor_x, anchor_y)):
        raise ValueError(f"Unsupported off-frame caption position on clip {ident}")
    content = style["content"]
    if not content.strip():
        return None
    # Floor the cue boundary to keep its quantized start/end on exact 30fps
    # frames rather than dropping a frame when rounding 66.667ms to 67ms.
    words = ()
    if karaoke:
        # Without ASR word timestamps, the native caption's exact clip
        # frames are apportioned by word length. This is approximate
        # pacing, not claimed to be speech-aligned transcription.
        tokens = content.split()
        frames = end - first
        if len(tokens) > frames:
            raise ValueError(f"Caption karaoke on {ident} needs at least one frame per word")
        remaining = frames
        weight_left = sum(max(1, len(token)) for token in tokens)
        start_frame = first
        built = []
        for index_word, token in enumerate(tokens):
            weight = max(1, len(token))
            left = len(tokens) - index_word - 1
            allot = (remaining if not left else
                     max(1, min(remaining - left, round(remaining * weight / weight_left))))
            stop_frame = start_frame + allot
            built.append(WordTiming(token, start_frame * 1000 // FPS,
                                    stop_frame * 1000 // FPS))
            start_frame = stop_frame
            remaining -= allot
            weight_left -= weight
        words = tuple(built)
    cue = Cue(index, first * 1000 // FPS, end * 1000 // FPS, content, words)
    caption_style = CaptionStyle(font=str(_hanken_font()), font_size=size, fill=fill,
                                 outline=outline, background=background,
                                 highlight=highlight,
                                 outline_width=stroke,
                                 padding=(12 if background[3] else 0), margin=0,
                                 plate_radius=(round(radius * HEIGHT) if background[3] else 0),
                                 anchor=(anchor_x, anchor_y), shadow=shadow,
                                 opacity=opacity * clip_opacity, track_rank=track_rank)
    return cue, caption_style


def load_project(root: Path, warn) -> Project:
    root = root.resolve(strict=True)
    manifest = next((root / name for name in ("concat.json", "wolfcut.json")
                     if (root / name).is_file()), None)
    if manifest is None:
        raise ValueError("Missing native Concat project")
    document = json.loads(manifest.read_text(encoding="utf-8-sig"))
    timeline = _validated_timeline(document)
    return _plan_project(document, timeline, lambda item: safe_media(root, item))


def _plan_project(document, timeline, resolve_media):
    tracks = {track["id"]: track for track in timeline["tracks"]}
    media = {item["id"]: item for item in document["media"]}
    lanes = {track: [] for track in tracks}
    sounds, captions, pending = {}, [], {}
    media_durations, native_cuts = {}, []
    track_order = {track: rank for rank, track in enumerate(tracks)}
    end_frame = 0
    for clip in timeline["clips"]:
        ident, kind = clip["id"], clip["kind"]
        track = tracks[clip["trackId"]]
        start = positive_time(clip.get("start"), f"clip start on {ident}")
        duration = positive_time(clip.get("duration"), f"clip duration on {ident}")
        first, end = round(start * FPS), round((start + duration) * FPS)
        if end <= first:
            raise ValueError(f"GPU clip {ident} has a sub-frame duration")
        end_frame = max(end_frame, end)
        span = FrameSpan(first, end)
        visible = kind != "audio" and track.get("visible", True)
        media_id = clip.get("mediaId")
        item = media.get(media_id, {}) if isinstance(media_id, str) else {}
        audible = (kind == "audio" or kind == "video" and item.get("hasAudio"))
        audible = audible and not (track.get("muted") or clip.get("muted"))
        if not visible and not audible:
            continue
        speed = number(clip.get("speed", 1), "speed")
        if not .01 <= speed <= 100:
            raise ValueError(f"Clip {ident} has unsupported playback speed")
        source = positive_time(clip.get("sourceStart", 0), "source start")
        if audible:
            sounds[ident] = Sound(resolve_media(item), first, source, (end-first)/FPS, speed,
                                  clip.get("audioStream"), number(clip.get("volume", 1), "volume"),
                                  positive_time(clip.get("fadeIn", 0), "fade in"),
                                  positive_time(clip.get("fadeOut", 0), "fade out"))
        if not visible:
            continue
        if kind == "text":
            rank = track_order[clip["trackId"]]
            caption = _caption(clip, first, end, len(captions) + 1, rank)
            if caption:
                captions.append((rank, span, ident, caption))
            continue
        defaults = Transform()
        constants = {name: number(clip.get(key, getattr(defaults, name)), key) for key, name in KEYS.items()}
        keys = {}
        for key in clip.get("keys") or ():
            name = KEYS.get(key.get("property"))
            if name is None:
                raise ValueError(f"Unsupported GPU key {key.get('property')} on {ident}")
            at = number(key.get("at"), f"key position on {ident}")
            if not 0 <= at <= 1:
                raise ValueError(f"Clip {ident} has a key outside its duration")
            keys.setdefault(name, {})[at] = (at, number(key.get("value"), f"key value on {ident}"), key.get("ease"))
        picture = Picture(ident, kind, None if kind == "layer" else resolve_media(item),
                          span, source, speed, constants,
                          {key: sorted(points.values()) for key, points in keys.items()},
                          filters_for(clip, start, duration), flip_h=bool(clip.get("flipH")),
                          flip_v=bool(clip.get("flipV")))
        lanes[clip["trackId"]].append(picture)
        media_durations[ident] = item.get("duration")
        if clip.get("transitionIn"):
            pending[ident] = clip["transitionIn"]

    sorted_lanes = []
    # Native track index zero is the bottom of the picture stack.
    for rank, lane in enumerate(lanes.values()):
        lane.sort(key=lambda clip: clip.span.start)
        for previous, incoming in zip(lane, lane[1:]):
            spec = pending.pop(incoming.id, None)
            if not spec:
                continue
            ident = spec["id"]
            span = incoming.span
            duration = positive_time(spec.get("duration", 1), "transition")
            intensity = (None if "intensity" not in spec else
                         number(spec["intensity"], f"GPU transition intensity on {incoming.id}"))
            if intensity is not None and not 0 <= intensity <= 1:
                raise ValueError(f"GPU transition intensity on {incoming.id} must be 0..1")
            if intensity is not None and ident not in NATIVE_TRANSITIONS:
                raise ValueError(f"GPU transition intensity is unsupported for {ident}")
            if previous.span.end != span.start or "layer" in (incoming.kind, previous.kind):
                raise ValueError(f"Unsupported or unaligned GPU transition {ident} on {incoming.id}")
            if ident in NATIVE_TRANSITIONS:
                native_cuts.append(_native_cut(rank, previous, incoming, ident, duration,
                                               media_durations[previous.id], intensity))
                continue
            mode = TRANSITIONS[ident]
            length = round(duration * FPS)
            if not 2 <= length <= span.end - span.start:
                raise ValueError(f"Unsupported or unaligned GPU transition {ident} on {incoming.id}")
            incoming.transition = Transition(mode[0], FrameSpan(span.start, span.start+length), mode[1])
            incoming.outgoing = previous
        sorted_lanes.append(tuple(lane))
    for ident, spec in pending.items():
        raise ValueError(f"GPU transition {spec.get('id')} lacks an adjacent clip on {ident}")
    native_cuts.sort(key=lambda cut: cut.transition.span.start)
    for first, second in zip(native_cuts, native_cuts[1:]):
        if first.transition.span.end > second.transition.span.start:
            raise ValueError(f"GPU transition on {second.incoming.id} overlaps another transition")
    for cut in native_cuts:
        previous = cut.outgoing.transition
        if previous and previous.span.end > cut.transition.span.start:
            raise ValueError(f"GPU transition on {cut.incoming.id} overlaps another transition")
        _native_cut_audio(sounds, cut)
    if not any(clip.path for lane in sorted_lanes for clip in lane):
        raise ValueError("The active timeline contains no visible video or images")
    # The worker composites captions above its picture stack. A caption
    # underneath a concurrent picture/layer would otherwise render on top.
    for rank, span, ident, _ in captions:
        for lane in sorted_lanes[rank:]:
            if any(clip.span.start < span.end and span.start < clip.span.end for clip in lane):
                raise ValueError(f"GPU caption {ident} overlaps video or effects on its track or above; "
                                 "move the caption to a higher track")
    return Project(tuple(sorted_lanes), tuple(caption for _, _, _, caption in sorted(
        captions, key=lambda entry: (entry[0], entry[1].start))), tuple(sounds.values()), end_frame,
        tuple(native_cuts))
