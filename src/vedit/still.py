"""`vedit still`: one annotated frame from a video, or an annotated copy of an image.

For illustrated guides. A small JSON spec names the moment (`at`), the regions to draw
the reader's eye to (`highlights`: box or ellipse outlines with an optional label), how
much to darken everything else (`dim`), an optional `crop`, a `max_width`, and a
labelled coordinate `grid` for the measuring pass — so an agent never composes
drawbox/geq/crop filtergraphs by hand.

Every coordinate is a FULL-FRAME pixel (or "NN%" of the frame), including when the
frame is cropped: the agent measures on the frame it looked at and the crop is applied
last, so nothing has to be re-derived after zooming in. The highlights must lie inside
the crop; a highlight the crop would remove is an error, not a silent omission.

A highlight is placed one of two ways. `"text": "RTools 4.5"` names the control's
on-screen text and OCR finds it (`ocr.locate`) — the agent never types pixels for
anything that is text, which is where every mis-placed box came from. `x`/`y`/`w`/`h`
is for what OCR cannot read (icons, light-on-dark button captions, terminal text),
measured on the gridded frame; those boxes get the grounding check after the render.

All the geometry (dim, outlines, grid lines) is one `geq` pass in rgb24. ffmpeg has no
ellipse filter, and drawbox/drawgrid only take YUV, so a single expression keeps the
frame in one colour space until the encoder.
"""

from __future__ import annotations

import functools
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import VeditError, ocr
from .geometry import Rect
from .media import MediaInfo, ffmpeg, probe, run
from .spec import _number, _reject_unknown, parse_time

OUTPUT_SUFFIXES = {".jpg", ".jpeg", ".png"}
# "source" and "output" are metadata the cli writes back into each still's sidecar (the
# spec saved beside the rendered image); accepted and ignored here so a sidecar is itself
# a valid spec.
TOP_LEVEL_KEYS = {"at", "highlights", "dim", "crop", "grid", "max_width", "pad", "notes",
                  "source", "output"}
HIGHLIGHT_KEYS = {"shape", "x", "y", "w", "h", "color", "thickness", "label", "pad",
                  "text", "near", "occurrence", "resolved"}
RECT_KEYS = {"x", "y", "w", "h"}
ANCHOR_KEYS = {"near", "occurrence", "resolved"}     # only meaningful beside "text"
MIN_TEXT = 3
CROP_KEYS = RECT_KEYS | {"margin"}
SHAPES = {"box", "ellipse"}
DEFAULT_GRID = 10
MAX_GRID = 25          # labels overlap past this
MAX_HIGHLIGHTS = 12    # a guide screenshot with more is unreadable; also bounds the geq expression
MIN_HIGHLIGHT = 4
MIN_CROP = 16
MAX_DIM = 0.95
MAX_PAD = 400
GRID_RGB = (255, 220, 0)
GRID_LINE = 2

# A colour name or an opaque hex value. No @alpha: the outline is drawn by geq, which
# needs the RGB triple, and a translucent outline is not something a guide wants.
COLOR_PATTERN = re.compile(r"^(?:#[0-9A-Fa-f]{6}|0x[0-9A-Fa-f]{6}|[A-Za-z][A-Za-z0-9]*)$")


@dataclass
class Highlight:
    shape: str
    rect: Rect                              # what is drawn: the target grown by pad (a box is
                                            # clamped to the frame so its ring stays closed; an
                                            # ellipse keeps its true centre and radii and is
                                            # simply not drawn where it leaves the frame)
    extent: Rect                            # rect clipped to the frame: what can be seen
    target: Rect                            # what the spec named
    pad: int
    color: str
    rgb: tuple[int, int, int]
    thickness: int
    label: str | None = None
    text: str | None = None                 # the on-screen text this box was placed on, if any
    resolved: Rect | None = None            # where OCR found it (== target); recorded in the sidecar


@dataclass
class StillSpec:
    at: float | None = None                 # the chosen frame's time; None for an image input
    frame: int | None = None                # its index, so the seek can aim between frames
    highlights: list[Highlight] = field(default_factory=list)
    dim: float = 0.0
    crop: Rect | None = None
    crop_note: str = ""
    grid: int = 0                           # divisions; 0 = off
    max_width: int | None = None
    notes: list[str] = field(default_factory=list)   # things worth printing with the plan


def frame_at(value, info: MediaInfo, *, where: str = "at") -> tuple[int, float]:
    """The frame index a time lands on, and that frame's own time.

    ffmpeg's -ss yields the first frame whose time is >= the seek point, so a time inside
    the last frame has no frame after it: refuse it now instead of an empty render later.
    """
    at = parse_time(value, info, where=where)
    frame = math.ceil(at * float(info.fps) - 1e-6)
    if at >= info.duration or frame >= info.total_frames:
        raise VeditError(f"{where}: {value!r} is at or past the last frame of the "
                         f"{info.duration:.2f}s source")
    return frame, frame / float(info.fps)


@functools.lru_cache(maxsize=None)
def rgb_of(color: str) -> tuple[int, int, int]:
    """Resolve a colour the way ffmpeg does, by asking ffmpeg.

    Exact: the source is forced to rgb24 before any YUV round trip (without the
    format filter, "red" comes back as 253,0,0).
    """
    proc = subprocess.run(
        [ffmpeg(), "-v", "error", "-f", "lavfi",
         "-i", f"color=c={color}:s=2x2:d=0.1,format=rgb24",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or len(proc.stdout) < 3:
        raise VeditError(f"{color!r} is not a colour ffmpeg knows")
    return tuple(proc.stdout[:3])  # type: ignore[return-value]


def _color(value, *, where: str) -> tuple[str, tuple[int, int, int]]:
    text = str(value)
    if not COLOR_PATTERN.match(text):
        raise VeditError(
            f'{where}: {value!r} is not a valid colour. Use a name ("red") or an opaque '
            f'hex value ("#ff8800"); alpha is not supported here.'
        )
    try:
        return text, rgb_of(text)
    except VeditError:
        raise VeditError(f"{where}: {value!r} is not a colour ffmpeg knows") from None


def _integer(value, *, where: str, low: int, high: int) -> int:
    """A whole number in range; 12.0 is fine, 12.5 is not — pixels do not come in halves."""
    number = _number(value, where=where)
    if number != int(number):
        raise VeditError(f"{where}: must be a whole number (got {value!r})")
    if not low <= number <= high:
        raise VeditError(f"{where}: must be between {low} and {high} (got {value!r})")
    return int(number)


def _coord(value, extent: int, *, where: str) -> int:
    """A pixel offset or size: a whole number, or "NN%" of the frame's extent."""
    if isinstance(value, bool):
        raise VeditError(f"{where}: expected a pixel value, got a boolean")
    if isinstance(value, str) and value.strip().endswith("%"):
        percent = _number(value.strip()[:-1], where=where)
        if not 0 <= percent <= 100:
            raise VeditError(f"{where}: {value!r} must be between 0% and 100%")
        return round(percent / 100 * extent)
    return _integer(value, where=where, low=0, high=max(extent, 1))


def _rect(item: dict, info: MediaInfo, *, where: str, minimum: int) -> Rect:
    missing = RECT_KEYS - set(item)
    if missing:
        raise VeditError(f'{where}: missing {sorted(missing)}; give "x", "y", "w" and "h" '
                         f"in full-frame pixels (or percentages like \"40%\")")
    x = _coord(item["x"], info.width, where=f"{where}.x")
    y = _coord(item["y"], info.height, where=f"{where}.y")
    w = _coord(item["w"], info.width, where=f"{where}.w")
    h = _coord(item["h"], info.height, where=f"{where}.h")
    if w < minimum or h < minimum:
        raise VeditError(f"{where}: must be at least {minimum}x{minimum} pixels (got {w}x{h})")
    if x + w > info.width:
        raise VeditError(f"{where}: x+w = {x + w} runs past the {info.width}-pixel-wide frame")
    if y + h > info.height:
        raise VeditError(f"{where}: y+h = {y + h} runs past the {info.height}-pixel-tall frame")
    return Rect(x, y, w, h)


def _anchor_text(item: dict, *, where: str) -> str:
    text = item["text"]
    if not isinstance(text, str) or len(text.strip()) < MIN_TEXT:
        raise VeditError(f"{where}.text: the control's on-screen text, at least {MIN_TEXT} "
                         f"characters (got {text!r})")
    clash = RECT_KEYS & set(item)
    if clash:
        raise VeditError(f'{where}: give either "text" (OCR places the box) or "x"/"y"/"w"/"h" '
                         f'(a box you measured), not both (got {sorted(clash)} beside "text")')
    return text.strip()


def _locate(item: dict, text: str, info: MediaInfo, spec: StillSpec, pad: int, *, where: str) -> Rect:
    """Where the anchor text is on the frame, via OCR; the sidecar's earlier answer is
    compared so a re-render that lands somewhere else is called out."""
    if not ocr.available():
        raise VeditError(f'{where}.text: placing a box by its text needs tesseract on PATH (and '
                         f'VEDIT_NO_OCR unset). Install it, or give "x", "y", "w", "h" measured '
                         f'on the gridded frame instead.')
    near = None
    if item.get("near") is not None:
        n = item["near"]
        if not isinstance(n, list) or len(n) != 2:
            raise VeditError(f"{where}.near: expected [x, y] in frame pixels (got {n!r})")
        near = (_coord(n[0], info.width, where=f"{where}.near[0]"),
                _coord(n[1], info.height, where=f"{where}.near[1]"))
    occurrence = None
    if item.get("occurrence") is not None:
        occurrence = _integer(item["occurrence"], where=f"{where}.occurrence", low=1, high=99)
    hit = ocr.locate(text, ocr.rows_for(info, spec.frame), near=near, occurrence=occurrence,
                     where=f"{where}.text")
    target = hit.rect
    previous = item.get("resolved")
    if isinstance(previous, dict) and RECT_KEYS <= set(previous):
        try:
            old = _rect(previous, info, where=f"{where}.resolved", minimum=1)
        except VeditError:
            old = None
        if old is not None:
            drift = max(abs(old.x - target.x), abs(old.y - target.y),
                        abs(old.right - target.right), abs(old.bottom - target.bottom))
            if drift > pad:
                spec.notes.append(
                    f"{where}: {text!r} now resolves to {ocr.describe(target)}, but the sidecar "
                    f"recorded {ocr.describe(old)} — the frame or the OCR reading changed by "
                    f"{drift} px; look at the result before embedding it")
    return target


def _bounding_box(rects: list[Rect]) -> Rect:
    x0 = min(r.x for r in rects)
    y0 = min(r.y for r in rects)
    x1 = max(r.right for r in rects)
    y1 = max(r.bottom for r in rects)
    return Rect(x0, y0, x1 - x0, y1 - y0)


def load(path: str | Path, info: MediaInfo) -> StillSpec:
    import json
    path = Path(path)
    if not path.exists():
        raise VeditError(f"spec file does not exist: {path}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise VeditError(f"{path.name} is not valid JSON: {exc}") from None
    return from_dict(raw, info, origin=path.name)


def from_dict(raw, info: MediaInfo, *, origin: str = "spec") -> StillSpec:
    if not isinstance(raw, dict):
        raise VeditError(f"{origin}: the spec must be a JSON object, got {type(raw).__name__}")
    _reject_unknown(raw, TOP_LEVEL_KEYS, where=origin)
    spec = StillSpec()

    if info.is_image:
        if "at" in raw:
            raise VeditError(f'{origin}: {info.path.name} is an image, so "at" does not apply; drop it')
    else:
        if "at" not in raw:
            raise VeditError(f'{origin}: needs "at" — the moment to take the frame from, '
                             f'e.g. "2:29" or 149')
        spec.frame, spec.at = frame_at(raw["at"], info)

    highlights = raw.get("highlights")
    if highlights is not None and not isinstance(highlights, list):
        raise VeditError(f'"highlights" must be a list, got {type(highlights).__name__}')
    if len(highlights or []) > MAX_HIGHLIGHTS:
        raise VeditError(f"highlights: at most {MAX_HIGHLIGHTS} per still (got {len(highlights)}); "
                         f"split the step into two screenshots")
    default_thickness = max(3, round(min(info.width, info.height) / 200))
    # The outline is drawn *outside* the named rectangle. Coordinates read off a grid are
    # good to a dozen pixels or so, and a box that hugs its target clips it on every such
    # miss; a box standing off by ~22 px (at 1080p) survives the miss and reads better —
    # it points at the control instead of framing it.
    default_pad = max(6, round(min(info.width, info.height) / 48))
    if raw.get("pad") is not None:
        default_pad = _integer(raw["pad"], where="pad", low=0, high=MAX_PAD)
    for index, item in enumerate(highlights or []):
        where = f"highlights[{index}]"
        if not isinstance(item, dict):
            raise VeditError(f'{where}: expected an object like '
                             f'{{"shape": "box", "x": 100, "y": 50, "w": 200, "h": 80}}, got {item!r}')
        _reject_unknown(item, HIGHLIGHT_KEYS, where=where)
        shape = str(item.get("shape", "box")).lower()
        if shape not in SHAPES:
            raise VeditError(f"{where}.shape: {shape!r} is not valid. Use one of {sorted(SHAPES)}")
        pad = default_pad
        if item.get("pad") is not None:
            pad = _integer(item["pad"], where=f"{where}.pad", low=0, high=MAX_PAD)
        text = None
        if "text" in item:
            text = _anchor_text(item, where=where)
            target = _locate(item, text, info, spec, pad, where=where)
        else:
            stray = ANCHOR_KEYS & set(item)
            if stray:
                raise VeditError(f'{where}: {sorted(stray)} only make sense beside "text"')
            if not RECT_KEYS & set(item):
                raise VeditError(
                    f'{where}: say where the box goes. Either "text": "<the control\'s on-screen '
                    f'text>" (OCR places it — use this for anything that is text) or "x", "y", '
                    f'"w", "h" in frame pixels measured on the gridded frame (for icons, buttons '
                    f'and terminal text OCR cannot read).'
                )
            target = _rect(item, info, where=where, minimum=MIN_HIGHLIGHT)
        grown = Rect(target.x - pad, target.y - pad, target.w + 2 * pad, target.h + 2 * pad)
        x0, y0 = max(0, grown.x), max(0, grown.y)
        x1, y1 = min(info.width, grown.right), min(info.height, grown.bottom)
        extent = Rect(x0, y0, x1 - x0, y1 - y0)
        rect = extent if shape == "box" else grown
        color, rgb = _color(item.get("color", "red"), where=f"{where}.color")
        thickness = default_thickness
        if item.get("thickness") is not None:
            thickness = _integer(item["thickness"], where=f"{where}.thickness", low=1, high=64)
        if 2 * thickness >= min(rect.w, rect.h):
            raise VeditError(f"{where}.thickness: {thickness} is too thick for a "
                             f"{rect.w}x{rect.h} highlight; the outline would fill it")
        label = None
        if "label" in item:
            if not isinstance(item["label"], str):
                raise VeditError(f"{where}.label: must be a string (got {item['label']!r})")
            label = item["label"].strip()
            if not label:
                raise VeditError(f"{where}.label: must not be empty; drop the key instead")
            if len(label) > 80:
                raise VeditError(f"{where}.label: keep it under 80 characters (got {len(label)})")
        elif text is not None:
            label = text
        spec.highlights.append(Highlight(shape=shape, rect=rect, extent=extent, target=target,
                                         pad=pad, color=color, rgb=rgb, thickness=thickness,
                                         label=label, text=text,
                                         resolved=target if text is not None else None))

    if "dim" in raw:
        dim = _number(raw["dim"], where="dim")
        if not 0 <= dim <= MAX_DIM:
            raise VeditError(f"dim: must be between 0 and {MAX_DIM} (got {dim:g}); "
                             f"it is the fraction of brightness removed outside the highlights")
        if dim > 0 and not spec.highlights:
            raise VeditError('dim has nothing to leave bright: add "highlights" or drop it')
        spec.dim = dim

    crop = raw.get("crop")
    if crop is not None:
        if not isinstance(crop, dict):
            raise VeditError('crop: expected an object like {"x": 0, "y": 0, "w": 800, "h": 600} '
                             f'or {{"margin": 40}}, got {crop!r}')
        _reject_unknown(crop, CROP_KEYS, where="crop")
        if "margin" in crop:
            if set(crop) != {"margin"}:
                raise VeditError('crop: give either "margin" (a box around the highlights) '
                                 'or "x"/"y"/"w"/"h", not both')
            if not spec.highlights:
                raise VeditError('crop.margin needs "highlights" to crop around')
            margin = _integer(crop["margin"], where="crop.margin", low=0, high=4000)
            box = _bounding_box([h.extent for h in spec.highlights])
            x0, y0 = box.x - margin, box.y - margin
            x1, y1 = box.right + margin, box.bottom + margin
            clamped = x0 < 0 or y0 < 0 or x1 > info.width or y1 > info.height
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(info.width, x1), min(info.height, y1)
            spec.crop = Rect(x0, y0, x1 - x0, y1 - y0)
            spec.crop_note = (f"margin {margin} around the highlights"
                              + (", clamped to the frame" if clamped else ""))
        else:
            spec.crop = _rect(crop, info, where="crop", minimum=MIN_CROP)
            for index, highlight in enumerate(spec.highlights):
                if not spec.crop.contains(highlight.extent):
                    r = highlight.extent
                    grown = f" (grown by its pad of {highlight.pad} to {r.x},{r.y} {r.w}x{r.h})" \
                        if highlight.pad else ""
                    raise VeditError(
                        f"highlights[{index}] ({highlight.target.x},{highlight.target.y} "
                        f"{highlight.target.w}x{highlight.target.h}){grown} lies outside the crop "
                        f"({spec.crop.x},{spec.crop.y} {spec.crop.w}x{spec.crop.h}) and would "
                        f"not be visible. Enlarge the crop, move the highlight, or lower pad."
                    )

    grid = raw.get("grid")
    if grid is True:
        spec.grid = DEFAULT_GRID
    elif grid is not None and grid is not False:
        divisions = _number(grid, where="grid")
        if divisions != int(divisions) or not 2 <= divisions <= MAX_GRID:
            raise VeditError(f"grid: use true (= {DEFAULT_GRID} divisions) or a whole number "
                             f"between 2 and {MAX_GRID} (got {grid!r})")
        spec.grid = int(divisions)

    if raw.get("max_width") is not None:
        spec.max_width = _integer(raw["max_width"], where="max_width", low=64, high=8192)

    return spec


# ---- geometry ------------------------------------------------------------------------

def output_size(spec: StillSpec, info: MediaInfo) -> tuple[int, int]:
    """The exact output dimensions: the invariant checked after rendering."""
    width, height = (spec.crop.w, spec.crop.h) if spec.crop else (info.width, info.height)
    if spec.max_width and width > spec.max_width:
        height = max(1, round(height * spec.max_width / width))
        width = spec.max_width
    return width, height


def _border_expr(h: Highlight) -> tuple[str, str]:
    """(outline mask, filled mask) as geq expressions in X and Y."""
    r, t = h.rect, h.thickness
    if h.shape == "box":
        outer = f"between(X,{r.x},{r.right - 1})*between(Y,{r.y},{r.bottom - 1})"
        inner = (f"between(X,{r.x + t},{r.right - 1 - t})"
                 f"*between(Y,{r.y + t},{r.bottom - 1 - t})")
    else:
        cx, cy = r.x + r.w / 2 - 0.5, r.y + r.h / 2 - 0.5
        rx, ry = r.w / 2, r.h / 2
        outer = f"lte(hypot((X-{cx:g})/{rx:g},(Y-{cy:g})/{ry:g}),1)"
        inner = f"lte(hypot((X-{cx:g})/{rx - t:g},(Y-{cy:g})/{ry - t:g}),1)"
    return f"({outer}*(1-{inner}))", f"({outer})"


def grid_lines(spec: StillSpec, info: MediaInfo) -> tuple[list[int], list[int]]:
    """The interior grid lines' pixel positions — one list, used by both mask and labels."""
    xs = [round(i * info.width / spec.grid) for i in range(1, spec.grid)]
    ys = [round(i * info.height / spec.grid) for i in range(1, spec.grid)]
    return xs, ys


def _geq(spec: StillSpec, info: MediaInfo) -> str | None:
    """One expression per channel: outlines over grid lines over the (dimmed) frame."""
    if not spec.highlights and not spec.grid:
        return None
    layers: list[tuple[str, tuple[int, int, int]]] = []
    filled: list[str] = []
    for h in spec.highlights:
        border, fill = _border_expr(h)
        layers.append((border, h.rgb))
        filled.append(fill)
    if spec.grid:
        xs, ys = grid_lines(spec, info)
        lines = ([f"between(X,{x},{x + GRID_LINE - 1})" for x in xs]
                 + [f"between(Y,{y},{y + GRID_LINE - 1})" for y in ys])
        layers.append(("(" + "+".join(lines) + ")", GRID_RGB))

    channels = []
    for index, name in enumerate("rgb"):
        base = f"{name}(X,Y)"
        if spec.dim:
            base = f"if({'+'.join(filled)},{base},{base}*{1 - spec.dim:g})"
        expr = base
        for mask, rgb in reversed(layers):
            expr = f"if({mask},{rgb[index]},{expr})"
        channels.append(f"{name}='{expr}'")
    return "geq=" + ":".join(channels)


def _label_filters(spec: StillSpec, info: MediaInfo, workdir: Path) -> list[str]:
    """A label sits just above its highlight, or below it when there is no room above.

    "Room" is measured against the crop when there is one — the label is drawn before
    the crop, and a label the crop then removes would be worse than none.
    """
    size = max(16, round(min(info.width, info.height) / 36))
    pad = max(4, size // 4)
    gap = pad + 6
    area = spec.crop or Rect(0, 0, info.width, info.height)
    filters = []
    for index, h in enumerate(spec.highlights):
        if h.label is None:
            continue
        text_file = workdir / f"label{index:02d}.txt"
        text_file.write_text(h.label)
        r = h.rect
        if r.y - size - 2 * pad - gap >= area.y:
            y = f"{r.y}-th-{gap}"
        elif r.bottom + size + 2 * pad + gap <= area.bottom:
            y = f"{r.bottom}+{gap}"
        else:
            y = f"{r.y + h.thickness + gap}"
        filters.append(
            f"drawtext=textfile={text_file}:expansion=none:fontsize={size}"
            f":fontcolor={h.color}:box=1:boxcolor=black@0.7:boxborderw={pad}"
            f":x='max({area.x + pad},min({r.x},{area.right}-tw-{pad}))':y='{y}'"
        )
    return filters


def _grid_labels(spec: StillSpec, info: MediaInfo) -> list[str]:
    """Pixel coordinates written beside every grid line (digits only, so no escaping)."""
    if not spec.grid:
        return []
    size = max(14, round(info.height / 45))
    style = f"fontsize={size}:fontcolor=yellow:box=1:boxcolor=black@0.6:boxborderw=3"
    # Labels hug the top and left edges of what survives the crop, for the lines inside it.
    area = spec.crop or Rect(0, 0, info.width, info.height)
    xs, ys = grid_lines(spec, info)
    filters = []
    for px in xs:
        if area.x <= px < area.right:
            filters.append(f"drawtext=text={px}:x={px + 4}:y={area.y + 4}:{style}")
    for py in ys:
        if area.y <= py < area.bottom:
            filters.append(f"drawtext=text={py}:x={area.x + 4}:y={py + 4}:{style}")
    return filters


def filtergraph(spec: StillSpec, info: MediaInfo, workdir: Path) -> str:
    filters = ["format=rgb24"]
    geq = _geq(spec, info)
    if geq:
        filters.append(geq)
    filters += _label_filters(spec, info, workdir)
    filters += _grid_labels(spec, info)
    if spec.crop:
        c = spec.crop
        filters.append(f"crop={c.w}:{c.h}:{c.x}:{c.y}")
    width, height = output_size(spec, info)
    source_w = spec.crop.w if spec.crop else info.width
    if width != source_w:
        filters.append(f"scale={width}:{height}:flags=lanczos")
    return ",".join(filters)


def command(spec: StillSpec, info: MediaInfo, out: Path, workdir: Path) -> list[str]:
    cmd = [ffmpeg(), "-y", "-v", "error"]
    if spec.frame is not None:
        # Aim half a frame early: -ss returns the first frame at or after the seek point,
        # and a seek to the frame's own time can land a rounding error past it.
        cmd += ["-ss", f"{max(0.0, (spec.frame - 0.5) / float(info.fps)):.6f}"]
    cmd += ["-i", str(info.path), "-frames:v", "1", "-update", "1",
            "-vf", filtergraph(spec, info, workdir)]
    if out.suffix.lower() in (".jpg", ".jpeg"):
        cmd += ["-q:v", "2"]
    cmd.append(str(out))
    return cmd


def plan(spec: StillSpec, info: MediaInfo) -> list[str]:
    if spec.at is None:
        lines = [f"input      {info.path.name}  {info.width}x{info.height}  (image)"]
    else:
        lines = [f"input      {info.path.name}  {info.width}x{info.height}  "
                 f"frame {spec.frame} at {spec.at:.3f}s"]
    for h in spec.highlights:
        t, r = h.target, h.extent
        line = f"highlight  {h.shape:<8} x {t.x} y {t.y} w {t.w} h {t.h}"
        if h.text is not None:
            line = f"highlight  {h.shape:<8} text {h.text!r} -> x {t.x} y {t.y} w {t.w} h {t.h} (OCR)"
        if h.pad:
            line += f"  pad {h.pad} -> x {r.x} y {r.y} w {r.w} h {r.h}"
        lines.append(line + f"  {h.color}  thickness {h.thickness}"
                     + (f"  {h.label!r}" if h.label else ""))
    if spec.dim:
        lines.append(f"dim        {spec.dim:g} of the brightness removed outside the highlights")
    if spec.grid:
        lines.append(f"grid       {spec.grid} divisions, labelled in full-frame pixels")
    if spec.crop:
        c = spec.crop
        lines.append(f"crop       x {c.x} y {c.y} w {c.w} h {c.h}"
                     + (f"   ({spec.crop_note})" if spec.crop_note else ""))
    width, height = output_size(spec, info)
    source_w = spec.crop.w if spec.crop else info.width
    if width != source_w:
        lines.append(f"scale      {width}x{height}  (max_width {spec.max_width})")
    lines.append(f"estimate   {width}x{height}")
    lines += [f"note       {note}" for note in spec.notes]
    return lines


def render(info: MediaInfo, spec: StillSpec, out: Path) -> Path:
    if out.suffix.lower() not in OUTPUT_SUFFIXES:
        raise VeditError(f"write a .jpg or .png (got {out.name})")
    if out.resolve() == info.path.resolve():
        raise VeditError("refusing to write over the input; choose a different -o path")
    out.parent.mkdir(parents=True, exist_ok=True)
    staged = out.with_name(f".vedit-{out.name}")
    # A leftover — or a planted symlink pointing at the source — must not be written through.
    if staged.is_symlink() or staged.exists():
        staged.unlink()
    workdir = Path(tempfile.mkdtemp(prefix="vedit-still-"))
    try:
        run(command(spec, info, staged, workdir), what="rendering the still")
        result = probe(staged, still=True)
        actual = (result.width, result.height)
        expected = output_size(spec, info)
        if actual != expected:
            raise VeditError(
                f"the still measures {actual[0]}x{actual[1]} but the plan said "
                f"{expected[0]}x{expected[1]}; refusing to install it — this is a bug in "
                f"vedit, not in the spec"
            )
        os.replace(staged, out)
    finally:
        if staged.exists():
            staged.unlink()
        shutil.rmtree(workdir, ignore_errors=True)
    return out
