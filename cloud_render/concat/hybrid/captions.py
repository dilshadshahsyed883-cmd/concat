"""SRT captions, karaoke timing, and cached CUDA subtitle composition.

Pillow rasterizes glyph textures; video frames and their blend stay on CUDA.
Cue and word boundaries are half-open intervals measured in milliseconds.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
import re
import unicodedata

from .contract import FrameRate, cuda, rgba


_TIMESTAMP = r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})"
_RANGE = re.compile(rf"{_TIMESTAMP}\s+-->\s+{_TIMESTAMP}")
_RGBA = tuple[int, int, int, int]


def _interval(start: int, end: int) -> None:
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ValueError("Caption intervals need increasing nonnegative integer milliseconds")


@dataclass(frozen=True)
class WordTiming:
    text: str
    start_ms: int
    end_ms: int

    def __post_init__(self):
        _interval(self.start_ms, self.end_ms)
        if not isinstance(self.text, str) or not re.fullmatch(r"\S+", self.text):
            raise ValueError("A karaoke timing needs exactly one word")


@dataclass(frozen=True)
class Cue:
    number: int
    start_ms: int
    end_ms: int
    text: str
    words: tuple[WordTiming, ...] = ()
    _word_starts: tuple[int, ...] = field(init=False, repr=False)

    def __post_init__(self):
        _interval(self.start_ms, self.end_ms)
        if type(self.number) is not int or self.number < 1:
            raise ValueError("Caption cue needs a positive integer number")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Caption cue needs text")
        words = tuple(self.words)
        if any(not isinstance(word, WordTiming) for word in words):
            raise TypeError("Caption cue words must be WordTiming values")
        if words and tuple(re.findall(r"\S+", self.text)) != tuple(word.text for word in words):
            raise ValueError(f"Cue {self.number}: timings must match its words in order")
        previous = self.start_ms
        for word in words:
            if word.start_ms < previous or word.end_ms > self.end_ms:
                raise ValueError(f"Cue {self.number}: word timings overlap or exceed the cue")
            previous = word.end_ms
        object.__setattr__(self, "words", words)
        object.__setattr__(self, "_word_starts", tuple(word.start_ms for word in words))

    def active_word(self, time_ms: int | Fraction) -> int | None:
        index = bisect_right(self._word_starts, time_ms) - 1
        if index >= 0 and time_ms < self.words[index].end_ms:
            return index
        return None


def _milliseconds(values: tuple[str, ...]) -> int:
    hours, minutes, seconds, ms = map(int, values)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + ms


def parse_srt(source: str, word_timings: Mapping[int, Sequence[WordTiming]] | None = None
              ) -> tuple[Cue, ...]:
    """Parse plain-text SRT, with optional word timings keyed by SRT cue number."""
    source = source.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if word_timings is None:
        word_timings = {}
    cues = []
    seen = set()
    for block in re.split(r"\n[^\S\n]*\n+", source) if source else ():
        lines = block.strip().split("\n")
        if len(lines) < 3 or not re.fullmatch(r"[0-9]+", lines[0].strip()):
            raise ValueError("Invalid SRT cue header or missing text")
        number = int(lines[0])
        if number in seen:
            raise ValueError(f"Duplicate SRT cue {number}")
        times = _RANGE.fullmatch(lines[1].strip())
        if times is None:
            raise ValueError(f"Cue {number}: invalid SRT timestamps")
        cues.append(Cue(number, _milliseconds(times.groups()[:4]),
                        _milliseconds(times.groups()[4:]), "\n".join(lines[2:]),
                        tuple(word_timings.get(number, ()))))
        seen.add(number)
    if set(word_timings) - seen:
        raise ValueError("Word timings reference an unknown SRT cue")
    return tuple(sorted(cues, key=lambda cue: (cue.start_ms, cue.number)))


@dataclass(frozen=True)
class CaptionStyle:
    font: str | None = None
    font_size: int = 48
    fill: _RGBA = (255, 255, 255, 255)
    highlight: _RGBA = (255, 218, 51, 255)
    outline: _RGBA = (0, 0, 0, 255)
    background: _RGBA = (0, 0, 0, 170)
    outline_width: int = 2
    padding: int = 10
    margin: int = 24
    placement: str = "bottom"

    def __post_init__(self):
        for color in (self.fill, self.highlight, self.outline, self.background):
            if (not isinstance(color, tuple) or len(color) != 4
                    or any(type(c) is not int or not 0 <= c <= 255 for c in color)):
                raise ValueError("Caption colors must be 8-bit RGBA tuples")
        sizes = (self.font_size, self.outline_width, self.padding, self.margin)
        limits = ((8, 512), (0, 32), (0, 128), (0, 512))
        if any(type(size) is not int or not low <= size <= high
               for size, (low, high) in zip(sizes, limits)):
            raise ValueError("Caption font size, outline, padding, or margin is out of range")
        if self.placement not in ("top", "bottom"):
            raise ValueError("Caption placement must be top or bottom")


def _direction(text: str) -> str | None:
    for char in text:
        bidi = unicodedata.bidirectional(char)
        if bidi == "L":
            return "ltr"
        if bidi in ("R", "AL"):
            return "rtl"
    return None


def _wrapped_lines(text, font, available):
    """Wrap at word boundaries and retain each line's first logical word."""
    index = 0
    lines = []
    for paragraph in text.splitlines():
        line = []
        first = index
        for word in paragraph.split():
            if font.getlength(word) > available:
                raise ValueError("Caption word exceeds the available width")
            if line and font.getlength(" ".join((*line, word))) > available:
                lines.append((" ".join(line), first, len(line)))
                line = []
                first = index
            line.append(word)
            index += 1
        if line:
            lines.append((" ".join(line), first, len(line)))
    return lines


def _glyph_texture(cue: Cue, active: int | None, style: CaptionStyle,
                   frame_width: int, frame_height: int):
    from PIL import Image, ImageDraw, ImageFont

    font = (ImageFont.truetype(style.font, style.font_size) if style.font
            else ImageFont.load_default(size=style.font_size))
    available = frame_width - 2 * (style.margin + style.padding + style.outline_width)
    if available < 1:
        raise ValueError("Frame too narrow for the caption style")
    lines = _wrapped_lines(cue.text, font, available)
    boxes = [font.getbbox(line, anchor="ls", stroke_width=style.outline_width)
             for line, _, _ in lines]
    width = max(right - left for left, _, right, _ in boxes) + 2 * style.padding
    ascent = max(-top for _, top, _, _ in boxes)
    descent = max(bottom for _, _, _, bottom in boxes)
    line_height = ascent + descent + max(1, style.font_size // 6)
    height = (len(lines) - 1) * line_height + ascent + descent + 2 * style.padding
    if width > frame_width - 2 * style.margin or height > frame_height - 2 * style.margin:
        raise ValueError("Caption exceeds the available frame area")

    texture = Image.new("RGBA", (width, height), style.background)
    fill_mask = Image.new("L", texture.size)
    outline_mask = Image.new("L", texture.size)
    highlight_box = None
    outline_draw, fill_draw = ImageDraw.Draw(outline_mask), ImageDraw.Draw(fill_mask)
    for row, ((line, first, count), (left, _, right, _)) in enumerate(zip(lines, boxes)):
        x = (width - (right - left)) // 2 - left
        y = style.padding + ascent + row * line_height
        outline_draw.text((x, y), line, fill=255, stroke_fill=255,
                          stroke_width=style.outline_width, font=font, anchor="ls")
        fill_draw.text((x, y), line, fill=255, font=font, anchor="ls")
        if active is not None and first <= active < first + count:
            words = tuple(re.finditer(r"\S+", line))
            word = words[active - first]
            directions = {_direction(token.group()) for token in words} - {None}
            if len(directions) > 1:
                raise ValueError("Karaoke requires a single writing direction per line")
            start, end = font.getlength(line[:word.start()]), font.getlength(line[:word.end()])
            if directions == {"rtl"}:
                advance = font.getlength(line)
                start, end = advance - end, advance - start
            gap = font.getlength(" ") / 2
            highlight_box = (max(0, round(x + start - gap)),
                             max(0, y - ascent), min(width, round(x + end + gap)),
                             min(height, y + descent))

    def color_layer(color, mask):
        layer = Image.new("RGBA", texture.size, color)
        layer.putalpha(mask.point(lambda value: value * color[3] // 255))
        return layer

    if style.outline_width:
        texture.alpha_composite(color_layer(style.outline, outline_mask))
    if highlight_box is not None:
        accent_mask = Image.new("L", texture.size)
        accent_mask.paste(fill_mask.crop(highlight_box), highlight_box)
        fill_mask.paste(0, highlight_box)
        texture.alpha_composite(color_layer(style.highlight, accent_mask))
    texture.alpha_composite(color_layer(style.fill, fill_mask))
    return texture


class CaptionRenderer:
    """Render caption cues onto a CUDA surface, caching bounded RGBA glyph tiles."""

    def __init__(self, cues: Sequence[Cue], style: CaptionStyle = CaptionStyle(),
                 texture_cache_bytes: int = 32 * 1024 * 1024):
        if type(texture_cache_bytes) is not int or texture_cache_bytes < 0:
            raise ValueError("Caption texture cache size must be a nonnegative integer")
        self.style = style
        self.cues = tuple(sorted(cues, key=lambda cue: (cue.start_ms, cue.number)))
        self._starts = tuple(cue.start_ms for cue in self.cues)
        self._latest_ends = []
        latest = 0
        for cue in self.cues:
            latest = max(latest, cue.end_ms)
            self._latest_ends.append(latest)
        self._cache_limit = texture_cache_bytes
        self._cache_bytes = 0
        self._textures = OrderedDict()

    def active(self, frame_index: int, rate: FrameRate) -> tuple[tuple[Cue, int | None], ...]:
        """Return visible cues and active words for the exact frame presentation time."""
        time_ms = rate.seconds(frame_index) * 1000
        matches = []
        for index in range(bisect_right(self._starts, time_ms) - 1, -1, -1):
            if self._latest_ends[index] <= time_ms:
                break
            cue = self.cues[index]
            if time_ms < cue.end_ms:
                matches.append((cue, cue.active_word(time_ms)))
        return tuple(reversed(matches))

    def _texture(self, cp, cue, word, width, height):
        key = (cp.cuda.Device().id, cue, word, self.style, width, height)
        existing = self._textures.get(key)
        if existing is not None:
            self._textures.move_to_end(key)
            return existing

        import numpy as np

        glyphs = np.asarray(_glyph_texture(cue, word, self.style, width, height))
        tile = cp.asarray(glyphs, blocking=True)
        if tile.nbytes <= self._cache_limit:
            while self._cache_bytes + tile.nbytes > self._cache_limit:
                _, oldest = self._textures.popitem(last=False)
                self._cache_bytes -= oldest.nbytes
            self._textures[key] = tile
            self._cache_bytes += tile.nbytes
        return tile

    def apply(self, frame, frame_index: int, rate: FrameRate):
        frame = rgba(frame)
        active = self.active(frame_index, rate)
        if not active:
            return frame

        cp = cuda()
        result = frame.copy()
        height, width = result.shape[:2]
        offset = self.style.margin
        for cue, word in reversed(active) if self.style.placement == "bottom" else active:
            tile = self._texture(cp, cue, word, width, height)
            rows, cols = tile.shape[:2]
            top = height - offset - rows if self.style.placement == "bottom" else offset
            offset += rows + self.style.padding
            if top >= height or top + rows <= 0:
                break
            left = (width - cols) // 2
            visible_top, visible_bottom = max(0, top), min(height, top + rows)
            foreground = tile[visible_top - top:visible_bottom - top].astype(cp.float32) / 255
            target = result[visible_top:visible_bottom, left:left + cols]
            src_alpha = foreground[..., 3:4]
            dst_alpha = target[..., 3:4] * (1 - src_alpha)
            alpha = src_alpha + dst_alpha
            rgb = foreground[..., :3] * src_alpha + target[..., :3] * dst_alpha
            target[..., :3] = rgb / cp.where(alpha > 0, alpha, 1)
            target[..., 3:4] = alpha
        return result
