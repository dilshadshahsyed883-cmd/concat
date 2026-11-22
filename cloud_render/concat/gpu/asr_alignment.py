"""Keep caption text and segment timing usable when ASR word alignment is imperfect.

Valid word boundaries stay exact. When explicitly enabled by the GPU caption
backend, an affected segment uses estimated karaoke intervals inside its real
speech boundaries. No source words or segment boundaries are invented.
"""
from __future__ import annotations

import math


class AsrWordAlignmentError(ValueError):
    """A model alignment problem that may merit one controlled ASR retry."""

    def __init__(self, reason, model, segment_index, word_index):
        # Never include spoken words, source paths or private audio in error logs.
        super().__init__(
            f"{reason} (model={model}, segment={segment_index}, word={word_index})"
        )


def _milliseconds(value, *, field, model, segment_index, word_index):
    if value is None or isinstance(value, (bool, str, bytes, complex)):
        raise AsrWordAlignmentError(
            f"Invalid ASR word {field}", model, segment_index, word_index
        )
    # NumPy arrays and bool scalars are not valid timestamps, even when
    # Python permits float(array([x])) or float(np.bool_(True)).
    if getattr(value, "ndim", 0) != 0 or getattr(getattr(value, "dtype", None), "kind", None) == "b":
        raise AsrWordAlignmentError(
            f"Invalid ASR word {field}", model, segment_index, word_index
        )
    try:
        # Accept safe numeric scalars such as numpy.float32/float64 as well as
        # Python floats. Whisper timestamps are converted into native ints.
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AsrWordAlignmentError(
            f"Invalid ASR word {field}", model, segment_index, word_index
        ) from exc
    if not math.isfinite(number) or number < 0:
        raise AsrWordAlignmentError(
            f"Invalid ASR word {field}", model, segment_index, word_index
        )
    return round(number * 1000)


def _aligned_words(segment, *, model, segment_index, previous_end, check_cancel):
    token_list = getattr(segment, "words", None) or ()
    segment_words = []
    leading_punctuation = ""
    for word_index, word in enumerate(token_list, 1):
        if check_cancel is not None:
            check_cancel()
        raw = getattr(word, "word", None)
        if not isinstance(raw, str):
            raise AsrWordAlignmentError(
                "Invalid ASR word text", model, segment_index, word_index
            )
        text = raw.strip()
        if not text:
            continue
        if len(text.split()) != 1:
            raise AsrWordAlignmentError(
                "ASR returned multi-word token", model, segment_index, word_index
            )
        try:
            start = _milliseconds(
                getattr(word, "start", None), field="start", model=model,
                segment_index=segment_index, word_index=word_index
            )
            end = _milliseconds(
                getattr(word, "end", None), field="end", model=model,
                segment_index=segment_index, word_index=word_index
            )
            if end <= start:
                raise AsrWordAlignmentError(
                    "Invalid ASR word interval", model, segment_index, word_index
                )
            if start < previous_end:
                raise AsrWordAlignmentError(
                    "Overlapping ASR word intervals", model, segment_index, word_index
                )
        except AsrWordAlignmentError:
            # Unaligned punctuation is not speech. Keep its visible text
            # adjacent to a genuinely timed word, with no invented interval.
            if not any(character.isalnum() for character in text):
                if segment_words:
                    segment_words[-1]["text"] += text
                else:
                    leading_punctuation += text
                continue
            raise

        if leading_punctuation:
            text = leading_punctuation + text
            leading_punctuation = ""
        segment_words.append({"text": text, "startMs": start, "endMs": end})
        previous_end = end

    if leading_punctuation:
        raise AsrWordAlignmentError(
            "Unaligned standalone punctuation", model, segment_index, 0
        )
    if not segment_words:
        # Never silently discard words in a spoken segment.
        if any(character.isalnum() for character in
               str(getattr(segment, "text", "") or "")):
            raise AsrWordAlignmentError(
                "Missing ASR word timestamps", model, segment_index, 0
            )
        return []
    return segment_words


def _segment_bounds(segment, *, model, segment_index, previous_end):
    try:
        start = _milliseconds(getattr(segment, "start", None), field="segment start",
                              model=model, segment_index=segment_index, word_index=0)
        end = _milliseconds(getattr(segment, "end", None), field="segment end",
                            model=model, segment_index=segment_index, word_index=0)
    except AsrWordAlignmentError as exc:
        raise AsrWordAlignmentError(
            "Invalid ASR segment interval", model, segment_index, 0
        ) from exc
    if start < previous_end or end <= start:
        raise AsrWordAlignmentError(
            "Invalid or overlapping ASR segment interval", model, segment_index, 0
        )
    return start, end


def _estimated_words(segment, *, model, segment_index, previous_end, check_cancel):
    start, end = _segment_bounds(segment, model=model, segment_index=segment_index,
                                 previous_end=previous_end)
    text = getattr(segment, "text", None)
    if not isinstance(text, str) or not any(c.isalnum() for c in text):
        raise AsrWordAlignmentError("Missing ASR segment text", model, segment_index, 0)
    tokens = text.split()
    # Every visible token needs at least one millisecond; do not extend the line.
    if len(tokens) > 10000 or end - start < len(tokens):
        raise AsrWordAlignmentError(
            "ASR segment too short for karaoke words", model, segment_index, 0
        )
    words = []
    for index, token in enumerate(tokens):
        if check_cancel is not None:
            check_cancel()
        words.append({
            "text": token,
            "startMs": start + (end - start) * index // len(tokens),
            "endMs": start + (end - start) * (index + 1) // len(tokens),
        })
    return words


def segments_to_cues(segments, *, model, check_cancel=None, allow_estimated=False):
    """Keep valid alignments, optionally estimating only an affected segment.

    Estimates use the ASR segment text and start/end, never the whole audio
    duration. Invalid segment boundaries still trigger the controlled ASR retry.
    """
    cues = []
    previous_end = 0
    estimated_segments = estimated_words = 0
    for segment_index, segment in enumerate(segments, 1):
        if check_cancel is not None:
            check_cancel()
        try:
            segment_words = _aligned_words(
                segment, model=model, segment_index=segment_index,
                previous_end=previous_end, check_cancel=check_cancel
            )
            if allow_estimated and segment_words:
                text = getattr(segment, "text", None)
                if isinstance(text, str) and text.strip():
                    if "".join(text.split()) != "".join(w["text"] for w in segment_words):
                        raise AsrWordAlignmentError(
                            "ASR words do not match segment text", model, segment_index, 0
                        )
                if hasattr(segment, "start") or hasattr(segment, "end"):
                    start, end = _segment_bounds(
                        segment, model=model, segment_index=segment_index,
                        previous_end=previous_end
                    )
                    if segment_words[0]["startMs"] < start or segment_words[-1]["endMs"] > end:
                        raise AsrWordAlignmentError(
                            "ASR words exceed segment interval", model, segment_index, 0
                        )
        except AsrWordAlignmentError:
            if not allow_estimated:
                raise
            segment_words = _estimated_words(
                segment, model=model, segment_index=segment_index,
                previous_end=previous_end, check_cancel=check_cancel
            )
            estimated_segments += 1
            estimated_words += len(segment_words)
        if not segment_words:
            continue
        previous_end = segment_words[-1]["endMs"]
        phrase = []
        for word in segment_words:
            if phrase and (len(phrase) >= 7 or
                    sum(len(entry["text"]) + 1 for entry in phrase)
                    + len(word["text"]) > 42):
                cues.append({
                    "startMs": phrase[0]["startMs"],
                    "endMs": phrase[-1]["endMs"],
                    "text": " ".join(entry["text"] for entry in phrase),
                    "words": phrase,
                })
                phrase = []
            phrase.append(word)
        if phrase:
            cues.append({
                "startMs": phrase[0]["startMs"],
                "endMs": phrase[-1]["endMs"],
                "text": " ".join(entry["text"] for entry in phrase),
                "words": phrase,
            })
    return {
        "version": 1, "cues": cues,
        "timing": ("segment-estimated-word-timestamps" if estimated_segments
                   else "model-word-timestamps"),
        "alignment": {"estimatedSegments": estimated_segments,
                      "estimatedWords": estimated_words},
    }
