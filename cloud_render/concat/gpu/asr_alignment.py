"""Turn faster-whisper word alignments into trustworthy GPU caption cues.

Do not interpolate missing speech timestamps or synthesize estimated karaoke
boundaries. Punctuation without its own alignment may be attached to an
adjacent *timed* word, but substantive speech must retain real model timings.
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


def segments_to_cues(segments, *, model, check_cancel=None):
    """Preserve word start/end and silence gaps; group actual timings for captions.

    Raises AsrWordAlignmentError for speech with missing/non-finite/overlapping
    intervals, allowing a controlled fresh ASR pass. Never guesses timing.
    """
    cues = []
    previous_end = 0
    for segment_index, segment in enumerate(segments, 1):
        if check_cancel is not None:
            check_cancel()
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
            continue
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
    return {"version": 1, "cues": cues}
