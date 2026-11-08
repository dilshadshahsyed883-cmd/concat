"""Bounded CUDA audio summing for the hybrid renderer.

FFmpeg decodes and pitch-preserving retimes each source to 48 kHz stereo PCM.
CuPy mixes gain, sample-accurate timeline offsets and fades on the GPU.
FFmpeg then limits/encodes AAC: NVENC is a video-only encoder.
"""
from __future__ import annotations

from contextlib import ExitStack
import subprocess
import tempfile

from .contract import cuda

SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_BYTES = 4
BLOCK_SAMPLES = 96_000
FRAME_SAMPLES = SAMPLE_RATE // 30


def tempo(speed):
    """Keep legacy FFmpeg atempo factors (which preserve audio pitch)."""
    factors = []
    while speed < 0.5:
        factors.append(0.5)
        speed *= 2
    while speed > 2:
        factors.append(2)
        speed /= 2
    return ",".join(f"atempo={factor:.9f}" for factor in (*factors, speed))


def decoder_command(sound):
    """Return a PCM-only decode/retime pipeline, with no CPU mixing."""
    stream = "0:a:0" if sound.stream is None else f"0:{int(sound.stream)}"
    filt = (f"atrim=duration={sound.seconds * sound.speed:.9f},"
            f"asetpts=PTS-STARTPTS,{tempo(sound.speed)},"
            "aresample=48000,aformat=channel_layouts=stereo")
    return ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
            "-ss", f"{sound.source_start:.9f}", "-i", str(sound.path),
            "-map", stream, "-filter:a", filt,
            "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
            "-f", "f32le", "-c:a", "pcm_f32le", "pipe:1"]


class DecodedSource:
    """Lazily stream one source; never materialize a full soundtrack in RAM."""
    def __init__(self, sound):
        self.sound = sound
        self.start = sound.start * FRAME_SAMPLES
        self.end = self.start + round(sound.seconds * SAMPLE_RATE)
        self.proc = None
        self.errors = None
        self.eof = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=5)
            if self.proc.stdout is not None:
                self.proc.stdout.close()
            self.proc = None
        if self.errors is not None:
            self.errors.close()
            self.errors = None

    def read(self, sample_count):
        if self.eof or not sample_count:
            return b""
        if self.proc is None:
            self.errors = tempfile.TemporaryFile()
            try:
                self.proc = subprocess.Popen(decoder_command(self.sound),
                                             stdout=subprocess.PIPE, stderr=self.errors)
            except BaseException:
                self.errors.close()
                self.errors = None
                raise
        data = self.proc.stdout.read(sample_count * CHANNELS * SAMPLE_BYTES)
        if len(data) % (CHANNELS * SAMPLE_BYTES):
            raise RuntimeError(f"Audio decoder returned an incomplete stereo sample: {self.sound.path}")
        if len(data) < sample_count * CHANNELS * SAMPLE_BYTES:
            self.eof = True
            code = self.proc.wait(timeout=30)
            if code:
                self.errors.seek(0)
                detail = self.errors.read(1800).decode("utf-8", "replace")
                raise RuntimeError(f"Audio decode failed for {self.sound.path}: {detail.strip()}")
        return data


def add_audio_cuda(target, samples, relative_start, sound, xp):
    """GPU gain envelope and additive float32 mixing (xp is NumPy in CPU-only tests)."""
    if not samples.size:
        return
    indices = (xp.arange(samples.shape[0], dtype=xp.float32) + relative_start) / SAMPLE_RATE
    gain = xp.full((samples.shape[0],), sound.volume, dtype=xp.float32)
    if sound.fade_in:
        gain *= xp.clip(indices / min(sound.fade_in, sound.seconds), 0, 1)
    if sound.fade_out:
        fade = min(sound.fade_out, sound.seconds)
        gain *= xp.clip((sound.seconds - indices) / fade, 0, 1)
    target += samples * gain[:, None]


def mixed_pcm(sounds, frames, *, backend=None, block_samples=BLOCK_SAMPLES):
    """Yield 48k stereo float32 PCM chunks, mixed on CUDA in production.

    Bounded to one output block plus one PCM chunk per overlapping sound.
    No CPU/NumPy fallback is used by production; backend is a test seam.
    """
    if block_samples <= 0:
        raise ValueError("Audio chunk size must be positive")
    xp = cuda() if backend is None else backend
    import numpy as np  # zero-copy view of FFmpeg's decoded PCM, not CPU mixing

    total = frames * FRAME_SAMPLES
    with ExitStack() as stack:
        sources = [stack.enter_context(DecodedSource(sound)) for sound in sounds]
        for left in range(0, total, block_samples):
            right = min(left + block_samples, total)
            mixed = xp.zeros((right - left, CHANNELS), dtype=xp.float32)
            for source in sources:
                overlap_left = max(left, source.start)
                overlap_right = min(right, source.end)
                if overlap_left >= overlap_right:
                    continue
                raw = source.read(overlap_right - overlap_left)
                if not raw:
                    continue
                samples = np.frombuffer(raw, dtype="<f4").reshape((-1, CHANNELS))
                gpu_samples = xp.asarray(samples, dtype=xp.float32)
                offset = overlap_left - left
                end = offset + len(samples)
                add_audio_cuda(mixed[offset:end], gpu_samples,
                               overlap_left - source.start, source.sound, xp)
            # Encoding requires host PCM; never mix it on the host.
            yield xp.asnumpy(mixed).astype("<f4", copy=False).tobytes()


def mux_cuda_audio(bitstream, output, sounds, frames, *, backend=None):
    """CUDA-mix audio into an H.264 bytestream; encode final AAC via FFmpeg."""
    if not sounds:
        raise ValueError("CUDA audio mux requires at least one sound")
    seconds = frames / 30
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
               "-f", "h264", "-r", "30", "-i", str(bitstream),
               "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
               "-i", "pipe:0", "-map", "0:v:0", "-map", "1:a:0",
               # Dynamic limiting remains in FFmpeg; no hard clipping.
               "-af", f"alimiter=latency=1,apad,atrim=duration={seconds:.9f}",
               "-c:a", "aac", "-b:a", "192k", "-c:v", "copy",
               "-video_track_timescale", "90000", "-movflags", "+faststart",
               "-n", str(output)]
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=errors)
        try:
            for pcm in mixed_pcm(sounds, frames, backend=backend):
                proc.stdin.write(pcm)
            proc.stdin.close()
            code = proc.wait(timeout=3600)
            if code:
                errors.seek(0)
                raise RuntimeError("FFmpeg audio encode/mux failed: "
                                   + errors.read(1800).decode("utf-8", "replace"))
        except BaseException as exc:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            if isinstance(exc, BrokenPipeError):
                errors.seek(0)
                raise RuntimeError("FFmpeg audio mux pipe closed: "
                                   + errors.read(1800).decode("utf-8", "replace")) from exc
            raise
        finally:
            proc.stdin.close()
