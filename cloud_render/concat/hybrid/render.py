"""Render a Concat timeline with GPU-resident picture frames and CPU audio."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import OrderedDict
from fractions import Fraction
from itertools import groupby
from pathlib import Path

from .animations import apply_transform
from .captions import CaptionRenderer
from .codec import Nvdec, Nvenc, nv12_from_rgba
from .compositor import Layer, composite
from .contract import FrameRate, cuda
from .filters import apply as filter_picture
from .project import FPS, HEIGHT, WIDTH, load_project
from .transitions import render_transition

RATE = FrameRate(FPS)


def invoke(args, timeout=3600):
    result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed: {result.stderr.strip()[-1800:]}")
    return result.stdout


class Sources:
    def __init__(self, temporary):
        self.temporary = temporary
        self.decoders = OrderedDict()
        self.stills = OrderedDict()
        self.rates = {}

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
        if path.suffix.lower() not in (".jpg", ".jpeg"):
            raise ValueError(f"{path.name}: CUDA still-image input must be a baseline JPEG")
        # NVDEC cannot feed an RGB JPEG directly into the video SDK wrapper.
        # Produce a single 4:2:0 H.264 frame entirely on the device and decode it there.
        with tempfile.TemporaryDirectory(dir=self.temporary) as scratch:
            encoded = Path(scratch) / "still.mp4"
            invoke(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                    "-init_hw_device", "cuda=gpu:0", "-filter_hw_device", "gpu",
                    "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                    "-c:v", "mjpeg_cuvid", "-i", str(path), "-frames:v", "1",
                    "-vf", "scale_cuda=w=trunc(iw/2)*2:h=trunc(ih/2)*2:format=nv12",
                    "-an", "-c:v", "h264_nvenc", "-y", str(encoded)], timeout=90)
            pixels = fitted(Nvdec(encoded).frame(0))
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
        offset = max(index - clip.span.start, 0) * clip.speed / FPS
        source = int((clip.source_start + offset) * self.rates[path] + 1e-7)
        if tail or source == len(decoder):
            source = min(source, len(decoder)-1)
        if source >= len(decoder):
            raise ValueError(f"{path.name}: timeline extends past available video frames")
        return fitted(decoder.frame(source))


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


def picture(sources, clip, index, tail=False):
    pixels = sources.image(clip.path) if clip.kind == "image" else sources.video(clip, index, tail)
    if clip.flip_h:
        pixels = pixels[:, ::-1]
    if clip.flip_v:
        pixels = pixels[::-1]
    pixels = apply_transform(pixels, clip.transform(index))
    for settings in clip.filters:
        pixels = filter_picture(pixels, settings)
    return pixels


def timeline_frames(plan, temporary, log):
    sources = Sources(temporary)
    captions = {}
    for cue, style in plan.captions:
        captions.setdefault(style, []).append(cue)
    captions = tuple(CaptionRenderer(cues, style) for style, cues in captions.items())
    starts = sorted(((clip.span.start, rank, clip) for rank, lane in enumerate(plan.lanes)
                     for clip in lane), key=lambda row: (row[0], row[1]))
    events = iter(groupby(starts, key=lambda row: row[0]))
    event = next(events, None)
    active = []
    for index in range(plan.frames):
        active = [item for item in active if item[1].span.end > index]
        if event is not None and event[0] == index:
            active.extend((rank, clip) for _, rank, clip in event[1])
            active.sort(key=lambda row: (row[0], row[1].span.start))
            event = next(events, None)
        layers = []
        for _, clip in active:
            if clip.kind == "layer":
                if clip.filters and layers:
                    below = composite(layers, width=WIDTH, height=HEIGHT, background=(0,0,0,1))
                    effect = below
                    for settings in clip.filters:
                        effect = filter_picture(effect, settings)
                    layers = [Layer(below), Layer(effect, opacity=clip.transform(index).opacity)]
                continue
            pixels = picture(sources, clip, index)
            if clip.transition and clip.transition.span.contains(index):
                outgoing = picture(sources, clip.outgoing, index, tail=True)
                pixels = render_transition(outgoing, pixels, clip.transition, index)
            layers.append(Layer(pixels))
        canvas = composite(layers, width=WIDTH, height=HEIGHT, background=(0,0,0,1))
        for renderer in captions:
            canvas = renderer.apply(canvas, index, RATE)
        if index and index % 300 == 0:
            log(f"CUDA rendered {index}/{plan.frames} frames")
        yield canvas


def tempo(speed):
    factors = []
    while speed < .5:
        factors.append(.5)
        speed *= 2
    while speed > 2:
        factors.append(2)
        speed /= 2
    return ",".join(f"atempo={factor:.9f}" for factor in (*factors, speed))


def mux(bitstream, output, sounds, frames):
    seconds = frames / FPS
    # Raw H.264 has no packet timestamps; FFmpeg input -r generates exact CFR
    # PTS. The -framerate option can yield 2700000/89999 instead of 30/1.
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "h264",
               "-r", str(FPS), "-i", str(bitstream)]
    chains = []
    for index, sound in enumerate(sounds, 1):
        command += ["-ss", f"{sound.source_start:.9f}", "-i", str(sound.path)]
        stream = f"{index}:a:0" if sound.stream is None else f"{index}:{int(sound.stream)}"
        filters = [f"atrim=duration={sound.seconds * sound.speed:.9f}", "asetpts=PTS-STARTPTS",
                   tempo(sound.speed), "aresample=48000",
                   "aformat=sample_fmts=fltp:channel_layouts=stereo", f"volume={sound.volume:.9f}"]
        if sound.fade_in:
            filters.append(f"afade=t=in:st=0:d={min(sound.fade_in,sound.seconds):.9f}")
        if sound.fade_out:
            fade = min(sound.fade_out, sound.seconds)
            filters.append(f"afade=t=out:st={sound.seconds-fade:.9f}:d={fade:.9f}")
        filters.append(f"adelay={sound.start * 1600}S:all=1")
        chains.append(f"[{stream}]{','.join(filters)}[a{index}]")
    if sounds:
        inputs = "".join(f"[a{index}]" for index in range(1, len(sounds)+1))
        chains.append(f"{inputs}amix=inputs={len(sounds)}:normalize=0,alimiter=latency=1,"
                      f"apad,atrim=duration={seconds:.9f}[audio]")
        command += ["-filter_complex", ";".join(chains), "-map", "0:v:0", "-map", "[audio]",
                    "-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-map", "0:v:0"]
    command += ["-c:v", "copy", "-video_track_timescale", "90000", "-movflags", "+faststart",
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
    plan = load_project(Path(project), log)
    output = Path(output)
    if output.suffix.lower() != ".mp4" or os.path.lexists(output):
        raise ValueError("The output must be a new MP4 path")
    cuda()
    log("Using NVDEC, CUDA effects and NVENC H.264. No software video fallback.")
    with tempfile.TemporaryDirectory(prefix=".concat-hybrid-", dir=output.parent) as temporary:
        temporary = Path(temporary)
        raw, result = temporary/"video.h264", temporary/"video.mp4"
        encoder = Nvenc(WIDTH, HEIGHT, RATE)
        written = 0
        with raw.open("wb") as handle:
            for index, frame in enumerate(timeline_frames(plan, temporary, log)):
                for packet in encoder.encode(nv12_from_rgba(frame), index):
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
        mux(raw, result, plan.sounds, plan.frames)
        verify(result, plan.frames, bool(plan.sounds))
        os.link(result, output)
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
