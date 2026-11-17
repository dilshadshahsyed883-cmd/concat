"""GPU speech recognition on the existing worker, with editable timed artifacts.

Only audio extraction runs through FFmpeg on CPU. ASR explicitly requires CUDA;
there is no CPU ASR or video-rendering fallback. Timings are model-derived, never
apportioned by character length. The source project is only changed in its
restored, worker-owned copy.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from cloud_render.concat.hybrid.captions import Cue, WordTiming

MODELS = {"tiny", "base", "small", "medium", "large-v3", "turbo"}
PRESETS = {"purple-word", "yellow-bar", "classic"}
ENGINE_VERSION = 1


class GpuSpeechUnavailable(RuntimeError):
    """An actionable status, distinct from successful local transcription."""


class CaptionCancelled(RuntimeError):
    pass


def _finite(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"Invalid {label}")
    return float(value)


def validate_request(value):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("GPU caption request needs version 1")
    allowed = {"version", "sourceClipId", "model", "language", "wordTimestamps", "preset", "renderAfter", "transcript", "style"}
    if set(value) - allowed:
        raise ValueError("Unknown GPU caption request settings")
    source = value.get("sourceClipId")
    if not isinstance(source, str) or not source or len(source) > 128:
        raise ValueError("Choose a narration/video source clip for GPU captions")
    model = value.get("model", "small")
    if model not in MODELS:
        raise ValueError("Unsupported GPU speech model")
    language = value.get("language")
    if language is not None and (not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}", language)):
        raise ValueError("Caption language must be a two/three-letter code or null")
    if value.get("preset", "purple-word") not in PRESETS:
        raise ValueError("Unknown GPU caption preset")
    if type(value.get("wordTimestamps", True)) is not bool or type(value.get("renderAfter", False)) is not bool:
        raise ValueError("Caption task options must be booleans")
    if value.get("preset", "purple-word") == "purple-word" and not value.get("wordTimestamps", True):
        raise ValueError("Purple word highlighting requires real word timestamps")
    if "style" in value and not isinstance(value["style"], dict):
        raise ValueError("Caption style overrides must be an object")
    return {"version": 1, "model": model, "language": language, "wordTimestamps": True,
            "preset": "purple-word", "renderAfter": False, **value}


def read_document(root):
    path = next((root/name for name in ("concat.json", "wolfcut.json") if (root/name).is_file()), None)
    if path is None or path.is_symlink():
        raise ValueError("GPU caption project manifest is missing")
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise ValueError("Invalid GPU caption project")
    return path, document


def active_timeline(document):
    timelines = document.get("timelines", [])
    if not isinstance(timelines, list) or not timelines:
        raise ValueError("GPU captions need a native timeline")
    return next((t for t in timelines if t.get("id") == document.get("activeTimelineId")), timelines[0])


def atomic_json(path, value):
    pending = path.with_suffix(path.suffix+".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(pending, path)


def validate_transcript(value, duration_ms, words_required=True):
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("cues"), list):
        raise ValueError("Timed transcript needs version 1 and a cues array")
    if len(value["cues"]) > 20000:
        raise ValueError("Timed transcript exceeds 20000 cues")
    cues = []
    previous = 0
    for index, record in enumerate(value["cues"], 1):
        if not isinstance(record, dict):
            raise ValueError("Invalid transcript cue")
        if not isinstance(record.get("words", []), list) or len(record.get("words", [])) > 10000:
            raise ValueError("Invalid transcript word list")
        words = tuple(WordTiming(word["text"], word["startMs"], word["endMs"])
                      for word in record.get("words", []))
        cue = Cue(index, record["startMs"], record["endMs"], record["text"], words)
        if cue.start_ms < previous or cue.end_ms > duration_ms:
            raise ValueError("Transcript cues overlap or exceed source duration")
        if words_required and not cue.words:
            raise ValueError("Real word timestamps are required; script-only alignment is unavailable")
        previous = cue.end_ms
        cues.append(cue)
    if not cues:
        raise ValueError("No speech captions were returned")
    return tuple(cues)


def transcript_data(cues):
    return {"version": 1, "timing": "model-word-timestamps", "cues": [
        {"startMs": c.start_ms, "endMs": c.end_ms, "text": c.text,
         "words": [{"text": w.text, "startMs": w.start_ms, "endMs": w.end_ms} for w in c.words]}
        for c in cues]}


def subtitle(cues, *, vtt=False, offset_ms=0):
    def stamp(ms):
        hours, ms = divmod(ms+offset_ms, 3600000)
        minutes, ms = divmod(ms, 60000)
        seconds, ms = divmod(ms, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02}{'.' if vtt else ','}{ms:03}"
    blocks = [f"{i}\n{stamp(c.start_ms)} --> {stamp(c.end_ms)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    return ("WEBVTT\n\n" if vtt else "")+"\n".join(blocks)


def ensure_not_cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise CaptionCancelled("GPU caption task cancelled; source bundle retained")


def extract_audio(root, document, request, destination, cancel=None):
    from cloud_render.concat.hybrid.project import safe_media
    timeline = active_timeline(document)
    clip = next((c for c in timeline.get("clips", []) if c.get("id") == request["sourceClipId"]), None)
    if clip is None or clip.get("kind") not in ("audio", "video"):
        raise ValueError("Caption source clip no longer contains audio/video")
    media = next((m for m in document.get("media", []) if m.get("id") == clip.get("mediaId")), None)
    if media is None:
        raise ValueError("Caption source media is missing")
    if clip.get("speedCurve"):
        raise ValueError("GPU captions do not yet support a variable-speed narration source")
    duration = _finite(clip.get("duration"), "caption source duration")
    speed = _finite(clip.get("speed", 1), "caption source speed")
    start = _finite(clip.get("sourceStart", 0), "caption source trim")
    timeline_start = _finite(clip.get("start", 0), "caption timeline start")
    if not .01 <= speed <= 100 or not 0 < duration <= 14400 or start < 0 or timeline_start < 0:
        raise ValueError("Caption source trim, duration or speed is out of range")
    source = safe_media(root, media)
    # FFmpeg audio is permitted on CPU; no video stream is decoded or filtered.
    factors = []
    remaining = speed
    while remaining > 2:
        factors.append(2.0); remaining /= 2
    while remaining < .5:
        factors.append(.5); remaining /= .5
    factors.append(remaining)
    filters = ",".join(f"atempo={factor:.12g}" for factor in factors)
    stream = clip.get("audioStream")
    if stream is not None and (type(stream) is not int or stream < 0):
        raise ValueError("Invalid caption audio stream")
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(start), "-t", str(duration*speed),
            "-i", str(source), "-map", f"0:{stream}" if stream is not None else "0:a:0",
            "-vn", "-af", filters, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destination)]
    ensure_not_cancelled(cancel)
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        while True:
            try:
                _, error = process.communicate(timeout=.25)
                break
            except subprocess.TimeoutExpired:
                ensure_not_cancelled(cancel)
        if process.returncode:
            raise RuntimeError("Caption audio extraction failed: "+error.decode(errors="replace")[-1000:])
    finally:
        if process.poll() is None:
            process.kill(); process.communicate()
    return duration, timeline_start


class FasterWhisperCuda:
    """Lazy CUDA-only model ownership; initialization is serialized by CaptionEngine."""
    identity = "faster-whisper:"+str(ENGINE_VERSION)

    def __init__(self):
        self.models = {}
        try:
            self.identity += ":"+importlib.metadata.version("faster-whisper")
        except importlib.metadata.PackageNotFoundError:
            self.identity += ":unavailable"

    @staticmethod
    def capability():
        try:
            import ctranslate2
            from faster_whisper import WhisperModel  # noqa: F401
            if ctranslate2.get_cuda_device_count() < 1:
                raise GpuSpeechUnavailable("No NVIDIA CUDA device is available for GPU speech recognition")
            return {"available": True, "backend": "faster-whisper", "device": "cuda"}
        except (ImportError, OSError, RuntimeError) as exc:
            return {"available": False, "backend": "faster-whisper", "device": "cuda", "reason": str(exc)}

    def transcribe(self, audio, request, cancel=None):
        capability = self.capability()
        if not capability["available"]:
            raise GpuSpeechUnavailable("GPU speech recognition unavailable: "+capability["reason"]+". Install the GPU ASR runtime or explicitly use local transcription.")
        from faster_whisper import WhisperModel
        key = request["model"]
        if key not in self.models:
            # Keep a single model resident when the user changes model size.
            self.models.clear()
            import gc
            gc.collect()
            self.models[key] = WhisperModel(key, device="cuda", compute_type="float16",
                download_root=os.environ.get("CONCAT_ASR_MODEL_CACHE", "/worker/asr-models"))
        segments, _ = self.models[key].transcribe(str(audio), language=request["language"],
            word_timestamps=True, vad_filter=True, beam_size=5)
        cues = []
        for segment in segments:
            ensure_not_cancelled(cancel)
            words = []
            for word in segment.words or ():
                text = word.word.strip()
                if not text:
                    continue
                if len(text.split()) != 1:
                    raise ValueError("ASR returned multi-word token without individual word timestamps")
                start, end = round(_finite(word.start, "ASR word start")*1000), round(_finite(word.end, "ASR word end")*1000)
                words.append({"text": text, "startMs": start, "endMs": end})
            if not words:
                continue
            # Boundaries derive from speech words, retaining actual silence gaps.
            phrase = []
            for word in words:
                if phrase and (len(phrase) >= 7 or sum(len(w["text"])+1 for w in phrase)+len(word["text"]) > 42):
                    cues.append({"startMs": phrase[0]["startMs"], "endMs": phrase[-1]["endMs"],
                                 "text": " ".join(w["text"] for w in phrase), "words": phrase})
                    phrase = []
                phrase.append(word)
            if phrase:
                cues.append({"startMs": phrase[0]["startMs"], "endMs": phrase[-1]["endMs"],
                             "text": " ".join(w["text"] for w in phrase), "words": phrase})
        return {"version": 1, "cues": cues}


class CaptionEngine:
    def __init__(self, cache, backend=None, extractor=extract_audio):
        self.cache = Path(cache)
        if self.cache.is_symlink():
            raise ValueError("Caption cache must not be a symlink")
        self.cache.mkdir(parents=True, exist_ok=True)
        self.backend = backend or FasterWhisperCuda()
        self.extractor = extractor
        self.lock = threading.Lock()

    def run(self, root, artifacts, status=lambda **kw: None, cancel=None):
        manifest, document = read_document(root)
        if document.get("gpuCaptionRequest") is None:
            return None
        request = validate_request(document["gpuCaptionRequest"])
        artifacts.mkdir(parents=True, exist_ok=True)
        audio = artifacts/"speech.wav"
        status(caption_state="extracting_audio")
        duration, timeline_start = self.extractor(root, document, request, audio, cancel)
        ensure_not_cancelled(cancel)
        with audio.open("rb") as audio_stream:
            audio_hash = hashlib.file_digest(audio_stream, "sha256").hexdigest()
        key = hashlib.sha256(json.dumps({"audio": audio_hash, "model": request["model"],
            "language": request["language"], "backend": self.backend.identity, "wordTimestamps": request["wordTimestamps"]}, sort_keys=True).encode()).hexdigest()
        cached = self.cache/(key+".json")
        started = time.perf_counter()
        cache_hit = False
        with self.lock:
            ensure_not_cancelled(cancel)
            if request.get("transcript") is not None:
                cues = validate_transcript(request["transcript"], round(duration*1000), request["wordTimestamps"])
                timing = "provided-word-timestamps"
            elif cached.is_file() and not cached.is_symlink():
                cues = validate_transcript(json.loads(cached.read_text()), round(duration*1000), request["wordTimestamps"])
                cache_hit = True
                timing = "model-word-timestamps"
            else:
                status(caption_state="transcribing")
                transcript = self.backend.transcribe(audio, request, cancel)
                status(caption_state="validating_word_alignment")
                cues = validate_transcript(transcript, round(duration*1000), request["wordTimestamps"])
                atomic_json(cached, transcript_data(cues))
                timing = "model-word-timestamps"
        ensure_not_cancelled(cancel)
        result = {**transcript_data(cues), "timing": timing, "cacheKey": key,
                  "sourceClipId": request["sourceClipId"], "timelineStartMs": round(timeline_start*1000),
                  "model": request["model"], "backend": self.backend.identity, "cacheHit": cache_hit,
                  "asrSeconds": round(time.perf_counter()-started, 4), "audioSeconds": duration,
                  "preset": request["preset"]}
        atomic_json(artifacts/"captions.json", result)
        (artifacts/"captions.srt").write_text(subtitle(cues, offset_ms=round(timeline_start*1000)), encoding="utf-8")
        (artifacts/"captions.vtt").write_text(subtitle(cues, vtt=True, offset_ms=round(timeline_start*1000)), encoding="utf-8")
        apply_captions(document, request, cues, timeline_start)
        atomic_json(manifest, document)
        audio.unlink(missing_ok=True)
        status(caption_state="captions_ready", caption_cache_hit=cache_hit, caption_timing=timing,
               caption_asr_seconds=result["asrSeconds"], caption_audio_seconds=duration)
        return {"render_after": request["renderAfter"], "result": result}


def apply_captions(document, request, cues, timeline_start):
    timeline = active_timeline(document)
    track_id = "gpu-caption-track"
    # Generated outputs are replaceable/idempotent; unrelated authored captions stay intact.
    timeline["clips"] = [c for c in timeline.get("clips", []) if c.get("gpuCaptionSource") != request["sourceClipId"]]
    if not any(t.get("id") == track_id for t in timeline["tracks"]):
        timeline["tracks"].append({"id": track_id, "visible": True, "muted": False})
    if request["preset"] == "purple-word":
        style = {"fontFamily": "Inter", "fontWeight": 900, "fontSize": .053, "color": "#ffffff",
                 "strokeColor": "#000000", "strokeWidth": .004, "shadow": True,
                 "karaoke": True, "karaokeColor": "#ffffff", "karaokeBackground": "#a020f0"}
        offset = .30
    elif request["preset"] == "yellow-bar":
        style = {"fontFamily": "Inter", "fontWeight": 900, "fontSize": .053, "color": "#000000",
                 "background": "#ffff00", "backgroundRadius": .012, "shadow": False, "karaoke": False}
        offset = -.35
    else:
        style = {"fontFamily": "Hanken Grotesk", "fontWeight": 600, "fontSize": .045,
                 "color": "#ffffff", "shadow": True, "karaoke": False}
        offset = .35
    style["maxWidth"] = .85
    style.update(request.get("style", {}))
    for index, cue in enumerate(cues, 1):
        upper = request["preset"] in ("purple-word", "yellow-bar")
        text = cue.text.upper() if upper else cue.text
        timeline["clips"].append({"id": f"gpu-caption-{request['sourceClipId']}-{index}",
            "trackId": track_id, "kind": "text", "name": text, "start": timeline_start+cue.start_ms/1000,
            "duration": (cue.end_ms-cue.start_ms)/1000, "sourceStart": 0, "mediaId": "",
            "offsetY": offset, "text": {**style, "content": text,
                "karaokeWords": [{"text": w.text.upper() if upper else w.text,
                    "startMs": w.start_ms-cue.start_ms, "endMs": w.end_ms-cue.start_ms} for w in cue.words]},
            "captionWords": [{"text": w.text.upper() if upper else w.text,
                "startMs": w.start_ms-cue.start_ms, "endMs": w.end_ms-cue.start_ms} for w in cue.words],
            "gpuCaptionSource": request["sourceClipId"]})
