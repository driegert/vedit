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

TOP_LEVEL_KEYS = {"cuts", "speed", "slides", "notes"}
SPEED_KEYS = {"range", "factor"}
SLIDE_KEYS = {"at", "seconds", "text", "image", "background", "color", "font_size"}

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
class EditSpec:
    cuts: list[tuple[float, float]] = field(default_factory=list)
    speeds: list[tuple[float, float, float]] = field(default_factory=list)  # (factor, start, stop)
    slides: list[Slide] = field(default_factory=list)


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
        missing = SPEED_KEYS - set(item)
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

    spec.slides.sort(key=lambda s: s.at)
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
