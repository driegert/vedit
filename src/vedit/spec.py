"""Parsing and validating the JSON edit spec.

The spec is written by agents that are not reliable at fiddly syntax, so validation
is deliberately strict and every message names the key it is complaining about.
Silently ignoring a malformed entry is the worst outcome here: it produces a video
that looks plausible but is not what was asked for.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import VeditError
from .media import MediaInfo

TOP_LEVEL_KEYS = {"cuts", "speed", "slides", "text", "chapters", "toc_card", "audio", "notes"}
SPEED_KEYS = {"range", "factor", "label"}      # "label" is optional
SPEED_REQUIRED = {"range", "factor"}
SLIDE_KEYS = {"at", "seconds", "text", "image", "background", "color", "font_size"}
TEXT_KEYS = {"range", "text", "position", "color", "background", "font_size"}
CHAPTER_KEYS = {"at", "title"}
TOC_KEYS = {"seconds", "title", "background", "color", "font_size"}
AUDIO_KEYS = {"normalize", "target"}

POSITIONS = {"top", "middle", "bottom"}
NORMALIZERS = {"ebu", "peak", "none"}

# ffmpeg colour: a name, or #RGB/#RRGGBB/#RRGGBBAA, either optionally with @alpha.
COLOR_PATTERN = re.compile(r"^(?:#[0-9A-Fa-f]{3,8}|[A-Za-z][A-Za-z0-9]*)(?:@[0-9]*\.?[0-9]+)?$")


def _number(value, *, where: str) -> float:
    """A float that is genuinely a finite number; booleans and NaN are rejected."""
    if isinstance(value, bool):
        raise VeditError(f"{where}: expected a number, got a boolean")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise VeditError(f"{where}: {value!r} is not a number") from None
    if not math.isfinite(number):
        raise VeditError(f"{where}: {value!r} is not a finite number")
    return number


def _color(value, *, where: str) -> str:
    """Colours are pasted into an ffmpeg filtergraph, so only safe forms are allowed."""
    text = str(value)
    if not COLOR_PATTERN.match(text):
        raise VeditError(
            f'{where}: {value!r} is not a valid colour. Use a name ("black"), '
            f'a hex value ("#204060"), optionally with an alpha ("black@0.5").'
        )
    return text


def parse_time(value, info: MediaInfo, *, where: str) -> float:
    """Accept 90, "90", "90s", "1:30", "1:02:03", "start", "end" -> seconds.

    A bare number means SECONDS, not frames: agents reason in seconds.
    """
    if isinstance(value, bool):
        raise VeditError(f"{where}: expected a time, got a boolean")
    if isinstance(value, (int, float)):
        seconds = _number(value, where=where)
    else:
        text = str(value).strip().lower()
        if text in ("start", "begin", "beginning"):
            return 0.0
        if text in ("end", "eof"):
            return info.duration
        for suffix in ("sec", "secs", "s"):
            if text.endswith(suffix) and ":" not in text:
                text = text[: -len(suffix)]
                break
        try:
            if ":" in text:
                parts = text.split(":")
                if len(parts) > 3 or any(not p.strip() for p in parts):
                    raise ValueError
                seconds = 0.0
                for power, part in enumerate(reversed(parts)):
                    seconds += float(part) * (60 ** power)
            else:
                seconds = float(text)
        except ValueError:
            raise VeditError(
                f'{where}: could not read {value!r} as a time. '
                f'Use seconds (90), "mm:ss" (1:30), "hh:mm:ss", or "end".'
            ) from None
        if not math.isfinite(seconds):
            raise VeditError(f"{where}: {value!r} is not a finite time")

    if seconds < 0:
        raise VeditError(f"{where}: negative times are not supported (got {value!r})")
    return seconds


def _range(value, info: MediaInfo, *, where: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise VeditError(f'{where}: expected a two-item range like ["1:30", "2:00"], got {value!r}')
    start = parse_time(value[0], info, where=f"{where} start")
    stop = parse_time(value[1], info, where=f"{where} stop")
    if stop <= start:
        raise VeditError(
            f"{where}: the range ends at or before it starts "
            f"({value[0]!r} -> {value[1]!r}). Ranges are [start, stop)."
        )
    # Clamping a too-late start would turn the range into a silent no-op, so say so.
    if start >= info.duration:
        raise VeditError(
            f"{where}: starts at {start:g}s but the video is only {info.duration:.2f}s long"
        )
    return start, min(stop, info.duration)


def _font_size(value, *, where: str) -> int | None:
    if value is None:
        return None
    size = int(_number(value, where=where))
    if not 4 <= size <= 512:
        raise VeditError(f"{where}: must be between 4 and 512 (got {size})")
    return size


def _items(raw: dict, key: str) -> list:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise VeditError(f'"{key}" must be a list, got {type(value).__name__}')
    return value


def _reject_unknown(item: dict, allowed: set[str], *, where: str) -> None:
    unknown = set(item) - allowed
    if unknown:
        raise VeditError(
            f"{where}: unknown key(s) {sorted(unknown)}. Valid keys are: {sorted(allowed)}"
        )


@dataclass
class Slide:
    at: float                      # seconds on the SOURCE timeline
    seconds: float = 3.0
    text: str | None = None
    image: Path | None = None
    background: str = "black"
    color: str = "white"
    font_size: int | None = None

    @property
    def label(self) -> str:
        if self.image is not None:
            return f"image {self.image.name}"
        first = (self.text or "").splitlines()[0] if self.text else ""
        return f"text {first!r}"


@dataclass
class Overlay:
    """Floating text burned over the video for a stretch of the SOURCE timeline."""

    start: float
    stop: float
    text: str
    position: str = "bottom"
    color: str = "white"
    background: str = "black@0.55"
    font_size: int | None = None


@dataclass
class Chapter:
    at: float
    title: str


@dataclass
class TocCard:
    seconds: float = 5.0
    title: str = "Contents"
    background: str = "black"
    color: str = "white"
    font_size: int | None = None


@dataclass
class Audio:
    normalize: str = "none"     # "ebu", "peak" or "none"
    target: float = -16.0       # LUFS for ebu, dBTP for peak (both true-peak measured)


@dataclass
class EditSpec:
    cuts: list[tuple[float, float]] = field(default_factory=list)
    speeds: list[tuple[float, float, float]] = field(default_factory=list)  # (factor, start, stop)
    slides: list[Slide] = field(default_factory=list)
    overlays: list[Overlay] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    toc_card: TocCard | None = None
    audio: Audio = field(default_factory=Audio)


def load(path: str | Path, info: MediaInfo) -> EditSpec:
    path = Path(path)
    if not path.exists():
        raise VeditError(f"spec file does not exist: {path}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise VeditError(f"{path.name} is not valid JSON: {exc}") from None
    return from_dict(raw, info, origin=path.name)


def from_dict(raw, info: MediaInfo, *, origin: str = "spec") -> EditSpec:
    if not isinstance(raw, dict):
        raise VeditError(f"{origin}: the spec must be a JSON object, got {type(raw).__name__}")
    _reject_unknown(raw, TOP_LEVEL_KEYS, where=origin)

    spec = EditSpec()

    for index, item in enumerate(_items(raw, "cuts")):
        spec.cuts.append(_range(item, info, where=f"cuts[{index}]"))

    for index, item in enumerate(_items(raw, "speed")):
        where = f"speed[{index}]"
        if not isinstance(item, dict):
            raise VeditError(
                f'{where}: expected an object like '
                f'{{"range": ["5:00", "8:00"], "factor": 2.0}}, got {item!r}'
            )
        _reject_unknown(item, SPEED_KEYS, where=where)
        missing = SPEED_REQUIRED - set(item)
        if missing:
            raise VeditError(f"{where}: missing {sorted(missing)}")
        start, stop = _range(item["range"], info, where=f"{where}.range")
        factor = _number(item["factor"], where=f"{where}.factor")
        if not 0 < factor <= 99999:
            raise VeditError(
                f"{where}.factor: must be greater than 0 (got {factor:g}). "
                f"Use a value above 1 to speed up, below 1 to slow down."
            )
        spec.speeds.append((factor, start, stop))
        if "label" in item:
            label = str(item["label"] or "").strip()
            if not label:
                raise VeditError(f"{where}.label: must not be empty; drop the key instead")
            # The common case: caption the sped-up stretch without restating its range.
            spec.overlays.append(Overlay(start=start, stop=stop, text=label))

    for index, item in enumerate(_items(raw, "slides")):
        where = f"slides[{index}]"
        if not isinstance(item, dict):
            raise VeditError(
                f'{where}: expected an object like {{"at": "8:00", "text": "Part 2"}}, got {item!r}'
            )
        _reject_unknown(item, SLIDE_KEYS, where=where)
        if "at" not in item:
            raise VeditError(f'{where}: needs an "at" time')

        has_text, has_image = bool(item.get("text")), bool(item.get("image"))
        if has_text == has_image:
            raise VeditError(
                f'{where}: give exactly one of "text" or "image" '
                f'(got {"both" if has_text else "neither"})'
            )
        image = None
        if has_image:
            image = Path(str(item["image"])).expanduser()
            if not image.exists():
                raise VeditError(f"{where}.image: file does not exist: {image}")

        seconds = _number(item.get("seconds", 3.0), where=f"{where}.seconds")
        if not 0 < seconds <= 600:
            raise VeditError(f"{where}.seconds: must be between 0 and 600 (got {seconds:g})")

        font_size = None
        if item.get("font_size") is not None:
            font_size = int(_number(item["font_size"], where=f"{where}.font_size"))
            if not 4 <= font_size <= 512:
                raise VeditError(f"{where}.font_size: must be between 4 and 512 (got {font_size})")

        spec.slides.append(
            Slide(
                at=min(parse_time(item["at"], info, where=f"{where}.at"), info.duration),
                seconds=seconds,
                text=str(item["text"]) if has_text else None,
                image=image,
                background=_color(item.get("background", "black"), where=f"{where}.background"),
                color=_color(item.get("color", "white"), where=f"{where}.color"),
                font_size=font_size,
            )
        )

    for index, item in enumerate(_items(raw, "text")):
        where = f"text[{index}]"
        if not isinstance(item, dict):
            raise VeditError(
                f'{where}: expected an object like '
                f'{{"range": ["5:00", "8:00"], "text": "..."}}, got {item!r}'
            )
        _reject_unknown(item, TEXT_KEYS, where=where)
        for required in ("range", "text"):
            if required not in item:
                raise VeditError(f'{where}: needs "{required}"')
        caption = str(item["text"] or "").strip()
        if not caption:
            raise VeditError(f"{where}.text: must not be empty")
        start, stop = _range(item["range"], info, where=f"{where}.range")
        position = str(item.get("position", "bottom")).lower()
        if position not in POSITIONS:
            raise VeditError(
                f"{where}.position: {position!r} is not valid. Use one of {sorted(POSITIONS)}"
            )
        spec.overlays.append(Overlay(
            start=start, stop=stop, text=caption, position=position,
            color=_color(item.get("color", "white"), where=f"{where}.color"),
            background=_color(item.get("background", "black@0.55"),
                              where=f"{where}.background"),
            font_size=_font_size(item.get("font_size"), where=f"{where}.font_size"),
        ))

    for index, item in enumerate(_items(raw, "chapters")):
        where = f"chapters[{index}]"
        if not isinstance(item, dict):
            raise VeditError(
                f'{where}: expected an object like {{"at": "5:00", "title": "..."}}, got {item!r}'
            )
        _reject_unknown(item, CHAPTER_KEYS, where=where)
        for required in ("at", "title"):
            if required not in item:
                raise VeditError(f'{where}: needs "{required}"')
        title = str(item["title"]).strip()
        if not title:
            raise VeditError(f"{where}.title: must not be empty")
        at = parse_time(item["at"], info, where=f"{where}.at")
        if at >= info.duration:
            raise VeditError(
                f"{where}.at: {item['at']!r} is at or past the end of the "
                f"{info.duration:.2f}s source"
            )
        spec.chapters.append(Chapter(at=at, title=title))

    toc = raw.get("toc_card")
    if toc:
        if toc is True:
            spec.toc_card = TocCard()
        elif isinstance(toc, dict):
            _reject_unknown(toc, TOC_KEYS, where="toc_card")
            seconds = _number(toc.get("seconds", 5.0), where="toc_card.seconds")
            if not 0 < seconds <= 600:
                raise VeditError(f"toc_card.seconds: must be between 0 and 600 (got {seconds:g})")
            spec.toc_card = TocCard(
                seconds=seconds,
                title=str(toc.get("title", "Contents")),
                background=_color(toc.get("background", "black"), where="toc_card.background"),
                color=_color(toc.get("color", "white"), where="toc_card.color"),
                font_size=_font_size(toc.get("font_size"), where="toc_card.font_size"),
            )
        else:
            raise VeditError(f"toc_card: expected true or an object, got {toc!r}")

    audio = raw.get("audio")
    if audio is not None:
        if not isinstance(audio, dict):
            raise VeditError(f'audio: expected an object like {{"normalize": "ebu"}}, got {audio!r}')
        _reject_unknown(audio, AUDIO_KEYS, where="audio")
        normalize = str(audio.get("normalize", "none")).lower()
        if normalize not in NORMALIZERS:
            raise VeditError(
                f"audio.normalize: {normalize!r} is not valid. Use one of {sorted(NORMALIZERS)}"
            )
        if "target" in audio and normalize == "none":
            raise VeditError(
                'audio.target has no effect without "normalize"; '
                'set normalize to "ebu" or "peak", or drop the target'
            )
        # -16 LUFS is a sensible speech target; peak normalising works in dBTP just
        # below clipping instead, so both default and range follow the mode chosen.
        # ffmpeg's loudnorm only accepts integrated targets down from -5 LUFS.
        default, low, high = ((-1.5, -70.0, 0.0) if normalize == "peak"
                              else (-16.0, -70.0, -5.0))
        target = _number(audio.get("target", default), where="audio.target")
        if not low <= target <= high:
            units = "dBTP" if normalize == "peak" else "LUFS"
            raise VeditError(
                f"audio.target: for {normalize!r} it must be between {low:g} and "
                f"{high:g} {units} (got {target:g})"
            )
        spec.audio = Audio(normalize=normalize, target=target)

    spec.slides.sort(key=lambda s: s.at)
    spec.chapters.sort(key=lambda c: c.at)
    spec.overlays.sort(key=lambda o: o.start)

    if spec.toc_card is not None and not spec.chapters:
        raise VeditError('toc_card needs "chapters" to list; add chapters or drop the card')
    if spec.audio.normalize != "none" and not info.has_audio:
        raise VeditError("audio.normalize was requested but the source has no audio stream")

    _check_speed_overlaps(spec)
    return spec


def _check_speed_overlaps(spec: EditSpec) -> None:
    """Overlapping speed ranges are ambiguous; auto-editor would pick one arbitrarily."""
    ordered = sorted(spec.speeds, key=lambda s: s[1])
    for (_, a_start, a_stop), (_, b_start, b_stop) in zip(ordered, ordered[1:]):
        if b_start < a_stop:
            raise VeditError(
                f"speed ranges overlap: [{a_start:g}s, {a_stop:g}s) and [{b_start:g}s, {b_stop:g}s). "
                f"Merge them into one entry."
            )
