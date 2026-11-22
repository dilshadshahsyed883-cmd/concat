"""Render a Concat timeline with CUDA video effects and CUDA audio mixing."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from collections import OrderedDict
from dataclasses import replace
from fractions import Fraction
from itertools import groupby
from pathlib import Path

from .animations import apply_transform
from .viewport import clip_viewport
from .canvas import make_background
from .canvas_shapes import render_shape, apply_mask
from .particles import ParticleEffect, apply as apply_particles
from .cuda_audio import mux_cuda_audio
from .captions import CaptionRenderer
from .codec import Nvdec, Nvenc, nv12_from_rgba
from .compositor import Layer, composite
from .contract import FrameRate, cuda
from .filters import apply as filter_picture, apply_contrast, apply_exposure
from .project import CONTRAST, EXPOSURE, FPS, HEIGHT, WIDTH, NativeColorEffect, load_project
from .performance import StageProfiler
from .transitions import render_transition

LOG = logging.getLogger(__name__)
RATE = FrameRate(FPS)


def invoke(args, timeout=3600):
    result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed: {result.stderr.strip()[-1800:]}")
    return result.stdout


STILL_IMAGE_EXTENSIONS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}
MAX_STILL_PIXELS = 100_000_000


def decode_still_rgba(path):
    """Validate and decode a source JPEG/PNG once; return straight RGBA uint8.

    CPU work is confined to still-image decompression. Video decode, fitting,
    compositing and final H.264 encoding remain CUDA/NVDEC/NVENC only.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError
    import numpy as np

    path = Path(path)
    expected = STILL_IMAGE_EXTENSIONS.get(path.suffix.lower())
    if expected is None:
        raise ValueError(f"{path.name}: unsupported still image; expected JPEG or PNG")
    try:
        with Image.open(path) as source:
            if source.format != expected:
                raise ValueError(f"expected {expected} image data, got {source.format or 'unknown'}")
            if source.width < 1 or source.height < 1 or source.width * source.height > MAX_STILL_PIXELS:
                raise ValueError("image dimensions exceed supported limits")
            # RGB, RGBA, grayscale, palette+tRNS and CMYK JPEG are normalized
            # without flattening the image onto an opaque matte.
            oriented = ImageOps.exif_transpose(source)
            return np.array(oriented.convert("RGBA"), dtype=np.uint8)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise ValueError(f"{path.name}: invalid {expected} still image ({error})") from error


class Sources:
    def __init__(self, temporary, profiler=None):
        self.temporary = temporary
        self.profiler = profiler if profiler is not None else StageProfiler()
        self.decoders = OrderedDict()
        self.stills = OrderedDict()
        # Only two fitted 1080p RGBA GPU surfaces are retained (about 63 MiB).
        # Repeated 24/25fps frames and transition holds avoid NVDEC and resize.
        self.video_frames = OrderedDict()
        self.rates = {}
        self.checked_video_spans = set()
        self.sizes = {}
        self.backgrounds = OrderedDict()
        self.shapes = OrderedDict()

    def shape(self, spec):
        value = self.shapes.pop(spec, None)
        if value is None: value = render_shape(spec, WIDTH, HEIGHT)
        self.shapes[spec] = value
        while len(self.shapes) > 4: self.shapes.popitem(last=False)
        return value

    def backdrop(self, clip, pixels):
        """Reuse still-image Canvas textures across frames without a reblur."""
        cache_key = ((clip.path, clip.flip_h, clip.flip_v, clip.background)
                     if clip.kind == "image" else None)
        if cache_key is not None:
            cached = self.backgrounds.pop(cache_key, None)
            if cached is not None:
                self.backgrounds[cache_key] = cached
                return cached
        dims = (self.dimensions(clip.path) if clip.background.kind == "blur"
                else (pixels.shape[1], pixels.shape[0]))
        backing = make_background(pixels, dims, clip.background)
        if cache_key is not None:
            if len(self.backgrounds) >= 3:
                self.backgrounds.popitem(last=False)
            self.backgrounds[cache_key] = backing
        return backing

    def dimensions(self, path):
        """Probe only once per media source; never download pixels to CPU."""
        if path not in self.sizes:
            stream = json.loads(invoke([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", str(path)
            ], timeout=30))["streams"][0]
            width, height = int(stream["width"]), int(stream["height"])
            if width < 1 or height < 1:
                raise ValueError(f"{path.name}: invalid source dimensions")
            self.sizes[path] = width, height
        return self.sizes[path]

    def decoder(self, path):
        decoder = self.decoders.pop(path, None)
        if decoder is None:
            if len(self.decoders) >= 4:
                self.decoders.popitem(last=False)
            decoder = Nvdec(path)
        self.decoders[path] = decoder
        return decoder

    def image(self, path):
        cached = self.stills.pop(path, None)
        if cached is not None:
            self.stills[path] = cached
            return cached
        # Still-image entropy decoding is CPU-side (Pillow); every transform,
        # alpha-aware resize, blend and output encode remains on CUDA/NVENC.
        # Unlike JPEG -> H.264 -> NVDEC, this retains PNG's original alpha.
        cp = cuda()
        pixels = cp.asarray(decode_still_rgba(path), dtype=cp.float32) / 255.0
        # Work in premultiplied alpha during GPU interpolation to prevent RGB
        # from transparent texels bleeding into visible PNG edges.
        pixels[..., :3] *= pixels[..., 3:4]
        pixels = fitted(pixels)
        alpha = pixels[..., 3:4]
        pixels[..., :3] = cp.where(
            alpha > 1e-6, pixels[..., :3] / cp.maximum(alpha, 1e-6), 0.0)
        if len(self.stills) >= 6:
            self.stills.popitem(last=False)
        self.stills[path] = pixels
        return pixels

    def video(self, clip, index, tail=False):
        path = clip.path
        if path not in self.rates:
            stream = json.loads(invoke(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                        "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                                        "-of", "json", str(path)], timeout=30))["streams"][0]
            rate = Fraction(stream["avg_frame_rate"])
            if rate <= 0 or abs(rate - Fraction(stream["r_frame_rate"])) > Fraction(1, 100):
                raise ValueError(f"{path.name}: a constant frame rate is required")
            self.rates[path] = rate
        decoder = self.decoder(path)
        frame_count = len(decoder)
        if frame_count < 1:
            raise ValueError(f"{path.name}: video has no decodable frames")
        rate = self.rates[path]
        # Probe the planned clip extent once it is first used. Project media
        # duration can be stale; decoder length is the actual source limit.
        hold_key = (clip.id, path, clip.span.start, clip.span.end,
                    clip.source_start, clip.speed)
        if not tail and hold_key not in self.checked_video_spans:
            self.checked_video_spans.add(hold_key)
            source_end = (clip.source_start
                          + (clip.span.end - clip.span.start) * clip.speed / FPS)
            last_offset = (clip.span.end - clip.span.start - 1) * clip.speed / FPS
            last_source = int(max(clip.source_start + last_offset, 0) * rate + 1e-7)
            if last_source >= frame_count:
                available = frame_count / float(rate)
                LOG.warning(
                    "Holding last video frame: clip=%s source=%s "
                    "available_frames=%d source_fps=%s available_seconds=%.6f "
                    "required_source_end=%.6f hold_timeline_seconds=%.6f",
                    clip.id, path.name, frame_count, rate, available, source_end,
                    min((clip.span.end - clip.span.start) / FPS,
                        max(0.0, source_end - available) / clip.speed),
                )
        offset = (index - clip.span.start) * clip.speed / FPS
        source = int(max(clip.source_start + offset, 0) * rate + 1e-7)
        # Preserve timeline length, audio and effects when the source runs out.
        # Reuse the last real GPU frame, including normal clips and transitions.
        source = min(source, frame_count - 1)

        key = (path, source)
        cached = self.video_frames.pop(key, None)
        if cached is not None:
            self.video_frames[key] = cached
            self.profiler.count("video_frame_cache_hit")
            return cached
        self.profiler.count("video_frame_cache_miss")
        with self.profiler.stage("nvdec_and_fit", index):
            pixels = fitted(decoder.frame(source))
        if len(self.video_frames) >= 2:
            self.video_frames.popitem(last=False)
        self.video_frames[key] = pixels
        return pixels


def fitted(pixels):
    cp = cuda()
    h, w = pixels.shape[:2]
    if (w, h) == (WIDTH, HEIGHT):
        return pixels
    from cupyx.scipy.ndimage import zoom

    scale = min(WIDTH / w, HEIGHT / h)
    width, height = max(1, round(w * scale)), max(1, round(h * scale))
    resized = zoom(pixels, (height / h, width / w, 1), order=1, prefilter=False)
    canvas = cp.zeros((HEIGHT, WIDTH, 4), dtype=cp.float32)
    x, y = (WIDTH-width)//2, (HEIGHT-height)//2
    canvas[y:y+height, x:x+width] = resized
    return canvas


NATIVE_COLOR_PASSES = {EXPOSURE: apply_exposure, CONTRAST: apply_contrast}


def picture(sources, clip, index, tail=False, *, force_opacity=False, placement_index=None,
            return_background=False):
    if clip.kind == "image":
        try:
            pixels = sources.image(clip.path)
        except (ValueError, OSError, RuntimeError) as error:
            raise ValueError(f"GPU clip {clip.id} image {clip.path}: {error}") from error
    else:
        pixels = sources.shape(clip.shape) if clip.kind == "shape" else sources.video(clip, index, tail)
    if clip.flip_h:
        pixels = pixels[:, ::-1]
    if clip.flip_v:
        pixels = pixels[::-1]
    # Canvas samples a separate untransformed copy of the clip. Its cover
    # remains centred while the foreground rotates, moves and animates.
    backing = (sources.backdrop(clip, pixels)
               if return_background and clip.background is not None else None)
    transform = clip.transform(index if placement_index is None else placement_index)
    if force_opacity:
        transform = replace(transform, opacity=1)
    if clip.mask is not None:
        pixels = apply_mask(pixels, clip.mask, sources.dimensions(clip.path), transform.scale)
    # Native packages run on the fitted picture before placement. Preserve
    # the old post-placement order for clips with only legacy filters.
    native_chain = any(isinstance(settings, (NativeColorEffect, ParticleEffect)) for settings in clip.filters)
    if not native_chain:
        pixels = apply_transform(pixels, transform)
    for settings in clip.filters:
        if isinstance(settings, ParticleEffect):
            pixels = apply_particles(pixels, settings, index)
        elif isinstance(settings, NativeColorEffect):
            handler = NATIVE_COLOR_PASSES[settings.id]
            pixels = handler(pixels, settings.at_frame(index))
        else:
            pixels = filter_picture(pixels, settings)
    foreground = apply_transform(pixels, transform) if native_chain else pixels
    if getattr(clip,"viewport",None) is not None:
        foreground = clip_viewport(foreground,clip.viewport)
    return (foreground, backing) if return_background else foreground


def picture_layers(sources, clip, index, *, with_canvas=False, **kwargs):
    """Background first, sharp foreground last, using the same decoded frame."""
    if with_canvas:
        foreground, backdrop = picture(sources, clip, index, return_background=True, **kwargs)
        return ([Layer(backdrop)] if backdrop is not None else []) + [Layer(foreground)]
    return [Layer(picture(sources, clip, index, **kwargs))]


def native_transition_frame(sources, layers, cut, index, background_rank=None):
    outgoing_index = cut.outgoing_frame(index)
    held = outgoing_index != index
    uses_canvas = cut.rank == background_rank
    outgoing_layers = [*layers, *picture_layers(
        sources, cut.outgoing, outgoing_index, with_canvas=uses_canvas, tail=True,
        placement_index=cut.outgoing.span.end if held else None)]
    incoming_layers = picture_layers(sources, cut.incoming, index,
                                     with_canvas=uses_canvas, force_opacity=True)
    canvas = dict(width=WIDTH, height=HEIGHT, background=(0, 0, 0, 1))
    outgoing_stack = composite(outgoing_layers, **canvas)
    incoming_stack = composite([*outgoing_layers, *incoming_layers], **canvas)
    return render_transition(outgoing_stack, incoming_stack, cut.transition, index)


def timeline_frames(plan, temporary, log, profiler=None):
    profiler = profiler if profiler is not None else StageProfiler()
    sources = Sources(temporary, profiler=profiler)
    captions = {}
    for cue, style in plan.captions:
        captions.setdefault(style, []).append(cue)
    captions = tuple(CaptionRenderer(cues, style) for style, cues in captions.items())
    starts = sorted(((clip.span.start, rank, clip) for rank, lane in enumerate(plan.lanes)
                     for clip in lane), key=lambda row: (row[0], row[1]))
    events = iter(groupby(starts, key=lambda row: row[0]))
    event = next(events, None)
    active = []
    cuts = iter(plan.native_cuts)
    cut = next(cuts, None)
    for index in range(plan.frames):
        if cut and index >= cut.transition.span.end:
            cut = next(cuts, None)
        active = [item for item in active if item[1].span.end > index]
        if event is not None and event[0] == index:
            active.extend((rank, clip) for _, rank, clip in event[1])
            active.sort(key=lambda row: (row[0], row[1].span.start))
            event = next(events, None)
        with profiler.stage("scene_build", index):
            layers = []
            for rank, clip in active:
                if (cut and rank == cut.rank and cut.transition.span.contains(index)
                        and (clip is cut.incoming or clip is cut.outgoing)):
                    layers = [Layer(native_transition_frame(
                        sources, layers, cut, index, plan.background_rank))]
                    continue
                if clip.kind == "layer":
                    if clip.filters and layers:
                        below = composite(layers, width=WIDTH, height=HEIGHT, background=(0,0,0,1))
                        effect = below
                        for settings in clip.filters:
                            effect = (apply_particles(effect, settings, index) if isinstance(settings, ParticleEffect)
                                      else filter_picture(effect, settings))
                        layers = [Layer(below), Layer(effect, opacity=clip.transform(index).opacity)]
                    continue
                clip_layers = picture_layers(
                    sources, clip, index,
                    with_canvas=((rank == plan.background_rank or clip.canvas_backdrop) and clip.kind in ("image", "video")))
                if clip.transition and clip.transition.span.contains(index):
                    outgoing = picture(sources, clip.outgoing, index, tail=True)
                    pixels = render_transition(outgoing, clip_layers[-1].texture,
                                               clip.transition, index)
                    clip_layers[-1] = Layer(pixels)
                layers.extend(clip_layers)
        with profiler.stage("composite", index):
            canvas = composite(layers, width=WIDTH, height=HEIGHT, background=(0,0,0,1))
        with profiler.stage("captions", index):
            for renderer in captions:
                canvas = renderer.apply(canvas, index, RATE)
        if index and index % 300 == 0:
            log(f"CUDA rendered {index}/{plan.frames} frames")
        yield canvas


def mux(bitstream, output, sounds, frames):
    # Raw H.264 requires input -r for exact CFR PTS; -framerate drifts.
    if sounds:
        # NVENC cannot encode audio. FFmpeg decodes/retimes PCM, CUDA sums
        # the audio stems, and FFmpeg encodes the final AAC soundtrack.
        return mux_cuda_audio(bitstream, output, sounds, frames)
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
               "-f", "h264", "-r", str(FPS), "-i", str(bitstream),
               "-map", "0:v:0", "-c:v", "copy",
               "-video_track_timescale", "90000", "-movflags", "+faststart",
               "-n", str(output)]
    invoke(command)


def verify(output, frames, has_audio):
    # Count demuxed packets without CPU video decoding. MP4 metadata can omit
    # nb_frames or express an exact 30 fps rate as an equivalent fraction.
    streams = json.loads(invoke(["ffprobe", "-v", "error", "-count_packets",
                                 "-show_streams", "-of", "json", str(output)],
                                timeout=60))["streams"]
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})

    def frame_count(field):
        value = video.get(field)
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    counts = {name: frame_count(name) for name in ("nb_frames", "nb_read_packets")
              if frame_count(name) is not None}
    try:
        actual_rate = Fraction(str(video.get("avg_frame_rate", "0/0")))
    except (ValueError, ZeroDivisionError):
        actual_rate = None

    expected = {"codec_name": "h264", "width": WIDTH, "height": HEIGHT,
                "fps": str(FPS), "frames": frames}
    actual = {"codec_name": video.get("codec_name"),
              "width": video.get("width"), "height": video.get("height"),
              "avg_frame_rate": video.get("avg_frame_rate"),
              "r_frame_rate": video.get("r_frame_rate"),
              "nb_frames": video.get("nb_frames"),
              "nb_read_packets": video.get("nb_read_packets"),
              "duration": video.get("duration")}
    correct = (video.get("codec_name") == "h264"
               and video.get("width") == WIDTH
               and video.get("height") == HEIGHT
               and actual_rate == FPS
               and bool(counts)
               and all(value == frames for value in counts.values()))
    if not correct:
        raise RuntimeError("Rendered MP4 video verification failed: expected "
                           + json.dumps(expected, sort_keys=True)
                           + "; actual " + json.dumps(actual, sort_keys=True))
    if abs(float(video["duration"]) - frames/FPS) > 1/FPS:
        raise RuntimeError("Rendered MP4 duration verification failed: expected "
                           + str(frames/FPS) + " seconds; actual "
                           + str(video.get("duration")))
    if has_audio:
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
        if (audio.get("codec_name") != "aac"
                or abs(float(audio["duration"]) - frames/FPS) > 1/FPS
                or abs(float(audio["start_time"]) - float(video["start_time"])) > 1/FPS):
            raise RuntimeError("Rendered MP4 audio verification failed: "
                               + json.dumps({"codec_name": audio.get("codec_name"),
                                             "start_time": audio.get("start_time"),
                                             "duration": audio.get("duration")},
                                            sort_keys=True))


def render_project(project, output, log=lambda message: print(message, file=sys.stderr, flush=True)):
    # Explicit opt-in only. Synchronized sample stages are diagnostics, not FPS benchmarks.
    profiler = StageProfiler.from_environment(
        synchronize=lambda: cuda().cuda.get_current_stream().synchronize())
    with profiler.stage("project_validation"):
        plan = load_project(Path(project), log)
    output = Path(output)
    if output.suffix.lower() != ".mp4" or os.path.lexists(output):
        raise ValueError("The output must be a new MP4 path")
    cuda()
    log("Using NVDEC, CUDA effects and NVENC H.264. No software video fallback.")
    if plan.sounds:
        log("Audio: CUDA PCM mixing; FFmpeg decodes, retimes, limits and encodes AAC.")
    with tempfile.TemporaryDirectory(prefix=".concat-hybrid-", dir=output.parent) as temporary:
        temporary = Path(temporary)
        raw, result = temporary/"video.h264", temporary/"video.mp4"
        encoder = Nvenc(WIDTH, HEIGHT, RATE)
        written = 0
        with raw.open("wb") as handle:
            for index, frame in enumerate(timeline_frames(plan, temporary, log, profiler)):
                with profiler.stage("nv12_pack", index):
                    packed = nv12_from_rgba(frame)
                with profiler.stage("nvenc", index):
                    packets = encoder.encode(packed, index)
                for packet in packets:
                    if packet.frame_index != written:
                        raise RuntimeError("NVENC returned a missing or reordered frame")
                    handle.write(packet.data)
                    written += 1
            for packet in encoder.finish():
                if packet.frame_index != written:
                    raise RuntimeError("NVENC returned a missing or reordered frame")
                handle.write(packet.data)
                written += 1
        if written != plan.frames:
            raise RuntimeError(f"NVENC returned {written} frames for a {plan.frames}-frame timeline")
        with profiler.stage("audio_mix_and_mux" if plan.sounds else "mux"):
            mux(raw, result, plan.sounds, plan.frames)
        with profiler.stage("output_verification"):
            verify(result, plan.frames, bool(plan.sounds))
        os.link(result, output)
    if profiler.enabled:
        log("CUDA stage diagnostics: " + json.dumps(profiler.summary(), sort_keys=True))
    return {"frames": plan.frames, "seconds": plan.frames/FPS, "encoder": "nvenc-h264"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(render_project(args.project, args.output)))
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"CUDA hybrid render failed: {error}\n")
