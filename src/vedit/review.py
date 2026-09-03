"""`vedit review`: a click-driven review page for a guide's screenshots.

The model picks a moment by the text on screen and OCR places the box, which leaves one
judgement nobody but the reader can make: is this the *useful* frame? A terminal that
has scrolled, a dialog caught a second early, a progress bar instead of the typed
command. Saying so used to mean typing to the model, whose next guess went through the
same pipeline. This page makes the correction a click.

`vedit review guide.qmd` walks the guide's images in order — grouping a Quarto
`::: {layout-ncol=N}` div's images into one step, side by side — and writes a page
beside it: each step's heading and captions, the current stills, the plain frame to
draw a box or crop on, and a few alternative moments. Every decision is recorded as an
ordered log in `review.json` — keep, use this moment, this box, this crop, add another
image, a note — and `--serve` applies moment/box/crop/add decisions on the spot by
editing the still's sidecar (or the guide itself, for a new image) and re-rendering, so
the page shows the result. Geometry never goes back through the model; the notes are
for it, once, at the end.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import VeditError
from .geometry import Rect
from .media import ffmpeg, probe, run

IMAGE_RE = re.compile(r"^!\[(?P<caption>[^\]]*)\]\((?P<path>[^)\s]+)\)(?:\{(?P<attrs>[^}\n]*)\})?", re.M)
HEADING_RE = re.compile(r"^#{2,4}\s+(?P<text>.+?)\s*$")
DIV_OPEN_RE = re.compile(r"^:::+\s*\{[^}]*layout-ncol[^}]*\}\s*$")
DIV_CLOSE_RE = re.compile(r"^:::+\s*$")
LAYOUT_NCOL_RE = re.compile(r"(layout-ncol=)(\d+)")
SUFFIX_LETTER_RE = re.compile(r"^(.+)-([b-z])$")
CANDIDATE_SPREAD = 3.0      # seconds either side of the current moment
SCENE_LEAD = 1.0            # a dialog finishes drawing about a second after the scene change
MAX_CANDIDATES = 4
DEFAULT_WIDTH = 960         # px; the frames on the page (and the drawing surface)
DEFAULT_PORT = 8765
DEFAULT_MARGIN = 160        # crop margin used around a drawn/added box in place of an explicit crop
MAX_SHOTS_PER_STEP = 3      # layout-ncol cap: a step may hold at most this many images side by side
DEFAULT_CAPTION = "(caption pending)"
RECT_KEYS = {"x", "y", "w", "h"}
ARROW_KEYS = ("x1", "y1", "x2", "y2")   # order matters: built into highlight dicts in this order


@dataclass
class Shot:
    key: str                 # "a", "b", "c"… position within the step (document order)
    caption: str
    image: Path               # absolute
    sidecar: Path              # image.with_suffix(".json")
    spec: dict | None         # sidecar contents if it exists and parses
    at: float | None          # spec["at"] as a float, else None
    attrs: str                # the {...} attribute text without the braces, "" if none
    line: int                 # 0-based line index of the image line in the .qmd
    candidates: list[float] = field(default_factory=list)
    plain: str = ""            # page-relative filename of the page-width frame at `at` ("" if at is None)
    thumbs: list[str] = field(default_factory=list)   # page-relative half-width frames, one per candidate
    boxes: list[dict] = field(default_factory=list)   # current highlights as full-frame rects:
        # [{"x","y","w","h","label"?}] -- measured ones as written; a text anchor contributes its
        # `resolved` rect (and its label) if it has one, else nothing. An arrow contributes
        # {"shape":"arrow","x1","y1","x2","y2","label"?} -- measured as written, or a text
        # anchor's cached `line`, else nothing.
    crop: dict | None = None   # the sidecar's crop as written: {x,y,w,h} | {"margin": N} | None


@dataclass
class Step:
    index: int                # 1-based, document order
    heading: str               # nearest ##/###/#### heading above (text only)
    shots: list[Shot]
    div: tuple[int, int] | None = None   # (open, close) 0-based line indexes of an enclosing
                                         # `::: {layout-ncol=N}` … `:::` div, else None


def _read_sidecar(image: Path) -> tuple[Path, dict | None, float | None]:
    sidecar = image.with_suffix(".json")
    spec = None
    if sidecar.exists():
        try:
            spec = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            spec = None
    at = None
    if isinstance(spec, dict) and spec.get("at") is not None:
        try:
            at = float(spec["at"])
        except (TypeError, ValueError):
            at = None
    return sidecar, spec, at


def _shot_boxes(spec: dict | None) -> list[dict]:
    """Current highlights as full-frame rects: a measured box as written; a text anchor
    contributes its `resolved` rect (and label) if OCR has placed it, else nothing. An
    arrow item contributes {"shape":"arrow","x1","y1","x2","y2","label"?} -- from its own
    `x1..y2` if measured, else from its cached `line` (a resolved text anchor), else
    nothing (not yet resolved)."""
    boxes = []
    for hi in (spec or {}).get("highlights") or []:
        if not isinstance(hi, dict):
            continue
        if hi.get("shape") == "arrow":
            if all(isinstance(hi.get(k), (int, float)) for k in ARROW_KEYS):
                rect = {"shape": "arrow", **{k: int(hi[k]) for k in ARROW_KEYS}}
            elif isinstance(hi.get("line"), dict) and all(isinstance(hi["line"].get(k), (int, float))
                                                           for k in ARROW_KEYS):
                line = hi["line"]
                rect = {"shape": "arrow", **{k: int(line[k]) for k in ARROW_KEYS}}
            else:
                continue
        elif RECT_KEYS <= set(hi):
            rect = {k: int(hi[k]) for k in ("x", "y", "w", "h")}
        elif "text" in hi and isinstance(hi.get("resolved"), dict) and RECT_KEYS <= set(hi["resolved"]):
            r = hi["resolved"]
            rect = {k: int(r[k]) for k in ("x", "y", "w", "h")}
        else:
            continue
        if hi.get("label"):
            rect["label"] = hi["label"]
        boxes.append(rect)
    return boxes


def _shot(qmd: Path, line: str, line_index: int, key: str) -> Shot:
    m = IMAGE_RE.match(line)
    image = (qmd.parent / unquote(m.group("path"))).resolve()
    sidecar, spec, at = _read_sidecar(image)
    crop = spec.get("crop") if isinstance(spec, dict) else None
    return Shot(key=key, caption=m.group("caption"), image=image, sidecar=sidecar, spec=spec,
               at=at, attrs=m.group("attrs") or "", line=line_index,
               boxes=_shot_boxes(spec), crop=crop)


def _guide_lines(qmd: Path) -> list[str]:
    """Split on "\n" only (not splitlines), so "\n".join gives the file back byte for byte
    and the indexes survive a stray "\r" or form feed."""
    with open(qmd, encoding="utf-8", newline="") as fh:
        return fh.read().split("\n")


def parse_guide(qmd: Path) -> list[Step]:
    """Every embedded image, in order, grouped by an enclosing layout div if any."""
    lines = _guide_lines(qmd)
    steps: list[Step] = []
    heading = ""
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        h = HEADING_RE.match(line)
        if h:
            heading = h.group("text")
            i += 1
            continue
        if DIV_OPEN_RE.match(line):
            close = next((j for j in range(i + 1, n) if DIV_CLOSE_RE.match(lines[j])), None)
            if close is None:                # unterminated div: not a group, keep scanning
                i += 1
                continue
            shots = []
            for j in range(i + 1, close):
                if IMAGE_RE.match(lines[j]):
                    shots.append(_shot(qmd, lines[j], j, chr(ord("a") + len(shots))))
            if shots:
                steps.append(Step(len(steps) + 1, heading, shots, div=(i, close)))
            i = close + 1
            continue
        if IMAGE_RE.match(line):
            steps.append(Step(len(steps) + 1, heading, [_shot(qmd, line, i, "a")], div=None))
        i += 1
    if not steps:
        raise VeditError(f"{qmd.name} embeds no images (lines like ![caption](path.jpg))")
    return steps


def source_video(steps: list[Step], qmd: Path, override: str | None) -> Path:
    if override:
        return Path(override)
    for s in steps:
        for shot in s.shots:
            if isinstance(shot.spec, dict) and shot.spec.get("source"):
                candidate = Path(shot.spec["source"])
                if not candidate.is_absolute():
                    candidate = qmd.parent / candidate
                if candidate.exists():
                    return candidate.resolve()
    raise VeditError("could not find the source video: no sidecar names one that exists; "
                     "pass --video")


def scene_changes(folder: Path) -> list[float]:
    """`scenes.txt` as the skill writes it: `<seconds> <score>` per line."""
    path = folder / "scenes.txt"
    if not path.exists():
        return []
    times = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            times.append(float(parts[0]))
        except ValueError:
            continue
    return sorted(times)


def candidate_times(at: float, scenes: list[float], duration: float) -> list[float]:
    """A few seconds either side, plus the screen change just before and just after."""
    raw = [at - CANDIDATE_SPREAD, at + CANDIDATE_SPREAD]
    before = [t for t in scenes if t < at - 1.0]
    after = [t for t in scenes if t > at + 1.0]
    if before:
        raw.append(before[-1] + SCENE_LEAD)
    if after:
        raw.append(after[0] + SCENE_LEAD)
    out: list[float] = []
    for t in raw:
        t = round(max(0.0, min(t, duration - 0.1)) * 2) / 2
        if abs(t - at) < 0.5 or any(abs(t - o) < 0.5 for o in out):
            continue
        out.append(t)
    return sorted(out)[:MAX_CANDIDATES]


def extract(video: Path, t: float, out: Path, width: int) -> None:
    run([ffmpeg(), "-y", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
         "-frames:v", "1", "-update", "1", "-vf", f"scale={width}:-2", "-q:v", "5", str(out)],
        what=f"extracting the frame at {t:g}s")


def frame_path(out_dir: Path, source: Path, t: float, width: int, *, prefix: str = "frame") -> Path:
    """The cached frame at `t` seconds, `width` px wide. Keyed by time (not by step), so a
    plain frame, a candidate thumbnail, and a /frame request for the same moment share one
    file; extraction is skipped when it already exists."""
    path = out_dir / f"{prefix}-{t:g}.jpg"
    if not path.exists():
        extract(source, t, path, width)
    return path


def build(qmd: Path, out_dir: Path, *, video: Path | None = None, width: int = DEFAULT_WIDTH,
          serve: bool = False) -> Path:
    """Write the review page and its frames; returns the page's path."""
    from .review_page import render_page

    qmd = qmd.resolve()
    steps = parse_guide(qmd)
    source = source_video(steps, qmd, str(video) if video else None)
    info = probe(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes = scene_changes(qmd.parent)
    for step in steps:
        for shot in step.shots:
            if shot.at is None:
                continue
            plain = frame_path(out_dir, source, shot.at, width)
            shot.plain = plain.name
            shot.candidates = candidate_times(shot.at, scenes, info.duration)
            for t in shot.candidates:
                thumb = frame_path(out_dir, source, t, width // 2, prefix="thumb")
                shot.thumbs.append(thumb.name)
    page = out_dir / "review.html"
    page.write_text(render_page(steps, out_dir, info, source, serve=serve), encoding="utf-8")
    manifest = {
        str(step.index): {
            "heading": step.heading,
            "div": step.div,
            "shots": [
                {"key": shot.key, "caption": shot.caption, "image": str(shot.image),
                 "sidecar": str(shot.sidecar), "at": shot.at, "line": shot.line,
                 "candidates": shot.candidates, "boxes": shot.boxes, "crop": shot.crop}
                for shot in step.shots
            ],
        }
        for step in steps
    }
    (out_dir / "manifest.json").write_text(json.dumps(
        {"guide": str(qmd), "video": str(source), "out_dir": str(out_dir.resolve()),
         "width": info.width, "height": info.height,
         "page_width": width, "serve": serve, "steps": manifest}, indent=2), encoding="utf-8")
    return page


# ---- applying decisions -------------------------------------------------------------------

def _shot_entry(manifest: dict, step: int, shot: int) -> dict:
    entry = manifest["steps"].get(str(step)) or manifest["steps"].get(step)
    if not entry:
        raise VeditError(f"step {step} is not in the review manifest")
    shots = entry["shots"]
    if not 0 <= shot < len(shots):
        raise VeditError(f"step {step} has no shot index {shot}")
    return shots[shot]


def _video_for(manifest: dict, spec: dict) -> Path:
    source = spec.get("source") or manifest.get("video")
    video = Path(source)
    if not video.is_absolute():
        video = Path(manifest["guide"]).parent / video
    return video


def _render(video: Path, spec: dict, image: Path) -> str:
    """Run `vedit still` to render/re-render `image` from `spec`; returns a note suffix
    (possibly empty). The sidecar is written by `vedit still` itself, from a scratch spec
    file distinct from it, exactly as a plain `vedit still` invocation would."""
    tmp = image.with_name(f".vedit-review-{image.stem}.json")
    tmp.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    try:
        proc = subprocess.run([sys.executable, "-m", "vedit.cli", "still", str(video), str(tmp),
                               "-o", str(image), "-q"], capture_output=True, text=True)
    finally:
        tmp.unlink(missing_ok=True)
    if proc.returncode != 0:
        err = next((l for l in proc.stderr.splitlines() if l.startswith("error:")), proc.stderr.strip()[-300:])
        raise VeditError(f"re-render failed: {err}")
    notes = [l for l in proc.stderr.splitlines() if l.startswith(("grounding", "note"))]
    return ("; " + notes[-1]) if notes else ""


ANCHOR_NOT_FOUND_RE = re.compile(r"highlights\[(?P<index>\d+)\]\.text: .* was not found on screen")


def _render_with_anchor_recovery(video: Path, spec: dict, image: Path) -> tuple[str, list[str]]:
    """Render, and if a text anchor no longer resolves on the (possibly new) frame, drop
    *that* anchor and try again -- at most once per anchor, so a genuine failure still
    surfaces. Returns (`_render`'s note suffix, the list of dropped anchor texts)."""
    dropped: list[str] = []
    budget = sum(1 for hi in spec.get("highlights") or [] if isinstance(hi, dict) and "text" in hi)
    while True:
        try:
            return _render(video, spec, image), dropped
        except VeditError as exc:
            m = ANCHOR_NOT_FOUND_RE.search(str(exc))
            if not m or budget <= 0:               # not an anchor miss, or every anchor already tried
                raise
            highlights = spec.get("highlights") or []
            index = int(m.group("index"))
            if not (0 <= index < len(highlights)) or not isinstance(highlights[index], dict) \
                    or "text" not in highlights[index]:
                raise
            budget -= 1
            dropped.append(str(highlights[index].get("text")))
            del highlights[index]
            if not highlights:                     # nothing left to dim around or crop to
                spec.pop("dim", None)
                if isinstance(spec.get("crop"), dict) and "margin" in spec["crop"]:
                    spec.pop("crop")


def _default_pad(frame_w: int, frame_h: int) -> int:
    """Mirrors still.py:281 -- the outline stands off the named rectangle so a box that
    hugs its target on a coarse grid reading still points at the control, not clips it."""
    return max(6, round(min(frame_w, frame_h) / 48))


def _pad_for(item: dict, spec: dict, frame_w: int, frame_h: int) -> int:
    if isinstance(item.get("pad"), (int, float)):
        return int(item["pad"])
    if isinstance(spec.get("pad"), (int, float)):
        return int(spec["pad"])
    return _default_pad(frame_w, frame_h)


def _padded_extent(rect: Rect, pad: int, frame_w: int, frame_h: int) -> Rect:
    """Mirrors still.py:312-315 -- grown by `pad` on every side, then clamped to the frame."""
    grown = Rect(rect.x - pad, rect.y - pad, rect.w + 2 * pad, rect.h + 2 * pad)
    x0, y0 = max(0, grown.x), max(0, grown.y)
    x1, y1 = min(frame_w, grown.right), min(frame_h, grown.bottom)
    return Rect(x0, y0, x1 - x0, y1 - y0)


def _highlight_extents(highlights: list, spec: dict, frame_w: int, frame_h: int) -> list[Rect]:
    """The padded, clamped extent of every highlight that has a known rect right now: a
    measured box as written, or a text anchor with a cached `resolved`. An anchor with no
    `resolved` (about to re-resolve, or never has) is ignored -- its position isn't known yet.
    An arrow contributes `still.arrow_extent(...)` (from its own `x1..y2`, or a text anchor's
    cached `line`) -- unpadded, since an arrow's extent formula already includes its head."""
    from .still import arrow_extent, arrow_thickness   # module import here avoids any import cycle

    extents = []
    for item in highlights or []:
        if not isinstance(item, dict):
            continue
        if item.get("shape") == "arrow":
            if all(isinstance(item.get(k), (int, float)) for k in ARROW_KEYS):
                x1, y1, x2, y2 = (int(item[k]) for k in ARROW_KEYS)
            elif isinstance(item.get("line"), dict) and all(isinstance(item["line"].get(k), (int, float))
                                                             for k in ARROW_KEYS):
                line = item["line"]
                x1, y1, x2, y2 = (int(line[k]) for k in ARROW_KEYS)
            else:
                continue
            thickness = int(item["thickness"]) if isinstance(item.get("thickness"), (int, float)) \
                else arrow_thickness(frame_w, frame_h)
            extents.append(arrow_extent(x1, y1, x2, y2, thickness, frame_w, frame_h))
            continue
        if RECT_KEYS <= set(item):
            rect = Rect(int(item["x"]), int(item["y"]), int(item["w"]), int(item["h"]))
        elif "text" in item and isinstance(item.get("resolved"), dict) and RECT_KEYS <= set(item["resolved"]):
            r = item["resolved"]
            rect = Rect(int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"]))
        else:
            continue
        extents.append(_padded_extent(rect, _pad_for(item, spec, frame_w, frame_h), frame_w, frame_h))
    return extents


def _union(rects: list[Rect]) -> Rect:
    x0 = min(r.x for r in rects)
    y0 = min(r.y for r in rects)
    x1 = max(r.right for r in rects)
    y1 = max(r.bottom for r in rects)
    return Rect(x0, y0, x1 - x0, y1 - y0)


def _grow_crop(crop_rect: Rect, highlights: list, spec: dict, frame_w: int, frame_h: int) -> tuple[Rect, bool]:
    """`crop_rect` grown just enough to contain every highlight's padded extent, clamped to
    the frame. Returns the (possibly unchanged) rect and whether it changed."""
    extents = _highlight_extents(highlights, spec, frame_w, frame_h)
    if not extents:
        return crop_rect, False
    bbox = _union(extents)
    if crop_rect.contains(bbox):
        return crop_rect, False
    combined = _union([crop_rect, bbox])
    x0, y0 = max(0, combined.x), max(0, combined.y)
    x1, y1 = min(frame_w, combined.right), min(frame_h, combined.bottom)
    return Rect(x0, y0, x1 - x0, y1 - y0), True


def _parse_boxes(raw: list) -> list[dict]:
    """`boxes` list items -> sidecar highlight dicts: a box/ellipse {x,y,w,h,label?}, or an
    arrow {"shape":"arrow","x1","y1","x2","y2","label"?}. Shared by `_apply_edit`,
    `apply_decision`'s legacy `box`, and `add_image`."""
    error = 'boxes[{}] needs x, y, w, h (or shape "arrow" with x1, y1, x2, y2)'
    highlights = []
    for i, b in enumerate(raw):
        if not isinstance(b, dict):
            raise VeditError(error.format(i))
        if b.get("shape") == "arrow":
            if not all(isinstance(b.get(k), (int, float)) for k in ARROW_KEYS):
                raise VeditError(error.format(i))
            hi = {"shape": "arrow", **{k: int(b[k]) for k in ARROW_KEYS}}
        elif all(isinstance(b.get(k), (int, float)) for k in ("x", "y", "w", "h")):
            hi = {k: int(b[k]) for k in ("x", "y", "w", "h")}
        else:
            raise VeditError(error.format(i))
        if b.get("label"):
            hi["label"] = str(b["label"])
        highlights.append(hi)
    return highlights


def _boxes_message(highlights: list[dict]) -> str:
    if len(highlights) == 1:
        h = highlights[0]
        if h.get("shape") == "arrow":
            return f"arrow set from x {h['x1']} y {h['y1']} to x {h['x2']} y {h['y2']}"
        return f"box set to x {h['x']} y {h['y']} w {h['w']} h {h['h']}"
    n_arrows = sum(1 for h in highlights if h.get("shape") == "arrow")
    n_boxes = len(highlights) - n_arrows
    if not n_arrows:
        return f"boxes set ({n_boxes})"
    parts = []
    if n_boxes:
        parts.append(f"{n_boxes} box" + ("" if n_boxes == 1 else "es"))
    parts.append(f"{n_arrows} arrow" + ("" if n_arrows == 1 else "s"))
    return f"boxes set ({', '.join(parts)})"


def _apply_edit(spec: dict, edit: dict, manifest: dict, video: Path, image: Path) -> str:
    """Mutate `spec` per `edit` (only the keys among "at"/"boxes"/"crop" that changed) and
    render once at the end. Returns the `; `-joined message."""
    frame_w = int(manifest.get("width") or 0)
    frame_h = int(manifest.get("height") or 0)
    parts: list[str] = []

    if "at" in edit:
        at = edit["at"]
        if not isinstance(at, (int, float)):
            raise VeditError("'at' must be numeric")
        spec["at"] = at
        parts.append(f"moment set to {at:g}s")

    if "boxes" in edit:
        boxes = edit["boxes"]
        if not isinstance(boxes, list):
            raise VeditError("'boxes' must be a list")
        highlights = _parse_boxes(boxes)
        spec["highlights"] = highlights
        if highlights:
            if any(h.get("shape") != "arrow" for h in highlights):
                spec.setdefault("dim", 0.35)
            else:
                spec.pop("dim", None)          # arrows leave nothing bright to dim around
            parts.append(_boxes_message(highlights))
        else:
            spec.pop("dim", None)                  # dim has nothing left to leave bright
            parts.append("highlights removed")
            crop = spec.get("crop")
            if isinstance(crop, dict) and "margin" in crop:
                spec.pop("crop", None)
                parts.append("crop removed (no highlights to bound the margin)")
    elif "at" in edit:
        for hi in spec.get("highlights") or []:
            if isinstance(hi, dict) and "text" in hi:
                hi.pop("resolved", None)          # re-resolve on the new frame

    if "crop" in edit:
        crop = edit["crop"]
        if crop is None:
            spec.pop("crop", None)
            parts.append("crop removed")
        elif isinstance(crop, dict) and "margin" in crop:
            spec["crop"] = {"margin": crop["margin"]}
            parts.append(f"crop margin set to {crop['margin']}")
        elif isinstance(crop, dict) and RECT_KEYS <= set(crop):
            rect = Rect(int(crop["x"]), int(crop["y"]), int(crop["w"]), int(crop["h"]))
            grown, changed = _grow_crop(rect, spec.get("highlights") or [], spec, frame_w, frame_h)
            spec["crop"] = {"x": grown.x, "y": grown.y, "w": grown.w, "h": grown.h}
            if changed:
                parts.append(f"crop grown to x {grown.x} y {grown.y} w {grown.w} h {grown.h} "
                             "to keep the boxes visible")
            else:
                parts.append(f"crop set to x {grown.x} y {grown.y} w {grown.w} h {grown.h}")
        else:
            raise VeditError("'crop' must be {x,y,w,h}, {margin: N}, or null")
    elif "boxes" in edit:
        crop = spec.get("crop")
        if isinstance(crop, dict) and "margin" not in crop and RECT_KEYS <= set(crop):
            rect = Rect(int(crop["x"]), int(crop["y"]), int(crop["w"]), int(crop["h"]))
            grown, changed = _grow_crop(rect, spec.get("highlights") or [], spec, frame_w, frame_h)
            if changed:
                spec["crop"] = {"x": grown.x, "y": grown.y, "w": grown.w, "h": grown.h}
                parts.append(f"crop grown to x {grown.x} y {grown.y} w {grown.w} h {grown.h} "
                             "to keep the boxes visible")

    if not spec.get("highlights"):
        spec.pop("dim", None)                      # nothing to leave bright
        if isinstance(spec.get("crop"), dict) and "margin" in spec["crop"]:
            spec.pop("crop")
            parts.append("crop removed (no highlights to bound the margin)")
    if not parts:
        return "note only"
    note, dropped = _render_with_anchor_recovery(video, spec, image)
    for text in dropped:
        parts.append(f"the anchor {text!r} is not on this frame, so its box was removed — draw one")
    return "; ".join(parts) + note


def apply_decision(manifest: dict, step: int, decision: dict) -> tuple[Path, str]:
    """Edit the shot's sidecar per the decision and re-render it. Returns (image, message).
    `moment`/`box`/`crop` are `edit` with one field (so old logs replay); `box` carries the
    previous label forward when the decision doesn't name one."""
    shot = decision.get("shot", 0)
    entry = _shot_entry(manifest, step, shot)
    sidecar = Path(entry["sidecar"])
    image = Path(entry["image"])
    if not sidecar.exists():
        raise VeditError(f"{image.name} has no sidecar to edit")
    spec = json.loads(sidecar.read_text(encoding="utf-8"))
    action = decision.get("action")
    if action == "keep":
        return image, "kept"

    if action == "moment":
        at = decision.get("at")
        if not isinstance(at, (int, float)):
            raise VeditError("a moment decision needs a numeric 'at'")
        edit = {"at": at}
    elif action == "box":
        box = decision.get("box") or {}
        highlight = _parse_boxes([box])[0]
        label = decision.get("label") or next(
            (hi.get("label") for hi in (spec.get("highlights") or []) if isinstance(hi, dict) and hi.get("label")),
            None)
        if label:
            highlight["label"] = label
        edit = {"boxes": [highlight]}
    elif action == "crop":
        if "crop" not in decision:
            raise VeditError("a crop decision needs a 'crop' key: {x,y,w,h}, {margin: N}, or null")
        edit = {"crop": decision["crop"]}
    elif action == "edit":
        edit = {k: decision[k] for k in ("at", "boxes", "crop") if k in decision}
    else:
        raise VeditError(f"unknown decision action {action!r}")

    video = _video_for(manifest, spec)
    message = _apply_edit(spec, edit, manifest, video, image)
    return image, message


def _check_guide_unchanged(qmd: Path, lines: list[str], first: Path, first_line: int,
                           div: tuple[int, int] | list | None) -> None:
    """The manifest's line numbers are only good while the guide is as it was built."""
    def bad(what: str) -> VeditError:
        return VeditError(f"{qmd.name} has changed since the review page was built ({what}); "
                          "rebuild the page")
    anchor = IMAGE_RE.match(lines[first_line]) if 0 <= first_line < len(lines) else None
    if anchor is None or (qmd.parent / unquote(anchor.group("path"))).resolve() != first.resolve():
        raise bad(f"line {first_line + 1} is no longer {first.name}")
    if div is not None:
        open_line, close_line = div
        if not (0 <= open_line < first_line < close_line < len(lines)
                and DIV_OPEN_RE.match(lines[open_line]) and DIV_CLOSE_RE.match(lines[close_line])):
            raise bad(f"lines {open_line + 1}-{close_line + 1} are no longer its layout div")


def _replace_text(path: Path, text: str) -> None:
    """Write through a sibling temp file and rename, so a reader never sees a truncated file."""
    tmp = path.with_name(f".{path.name}.vedit-tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def add_image(manifest: dict, step: int, decision: dict) -> tuple[Path, str]:
    """Render a new shot for `step` and wire it into the guide as a `layout-ncol` div,
    creating the div around the step's existing image if there isn't one yet."""
    entry = manifest["steps"].get(str(step)) or manifest["steps"].get(step)
    if not entry:
        raise VeditError(f"step {step} is not in the review manifest")
    shots = entry["shots"]
    if not shots:
        raise VeditError(f"step {step} has no images to add beside")
    if len(shots) >= MAX_SHOTS_PER_STEP:
        raise VeditError(f"step {step} already has {len(shots)} images; at most "
                         f"{MAX_SHOTS_PER_STEP} side by side are supported")
    at = decision.get("at")
    if not isinstance(at, (int, float)):
        raise VeditError("an add decision needs a numeric 'at'")

    first = Path(shots[0]["image"])
    m = SUFFIX_LETTER_RE.match(first.stem)
    base_stem = m.group(1) if m else first.stem
    new_image = None
    for letter in "bcdefghijklmnopqrstuvwxyz":
        candidate = first.with_name(f"{base_stem}-{letter}{first.suffix}")
        taken = (candidate, candidate.with_suffix(".json"),
                 candidate.with_name(f"{candidate.stem}.check{candidate.suffix}"))
        if not any(t.exists() for t in taken):      # an orphan sidecar would be overwritten too
            new_image = candidate
            break
    if new_image is None:
        raise VeditError(f"no free image name for {base_stem}{first.suffix} (b..z all taken)")

    box = decision.get("box")
    boxes_field = decision.get("boxes")
    spec: dict = {"at": at, "max_width": 1280}
    highlights: list[dict] = []
    if boxes_field is not None:
        if not isinstance(boxes_field, list):
            raise VeditError("an add decision's 'boxes' must be a list")
        highlights = _parse_boxes(boxes_field)
    elif box is not None:
        highlight = _parse_boxes([box])[0]
        label = decision.get("label")
        if label:
            highlight["label"] = label
        highlights.append(highlight)
    if highlights:
        spec["highlights"] = highlights
        if any(h.get("shape") != "arrow" for h in highlights):
            spec["dim"] = 0.35
    if "crop" in decision:
        if decision["crop"] is not None:       # an explicit null is "no crop"
            spec["crop"] = decision["crop"]
    elif highlights:
        spec["crop"] = {"margin": DEFAULT_MARGIN}

    caption = decision.get("caption") or DEFAULT_CAPTION
    qmd = Path(manifest["guide"])
    first_line = shots[0]["line"]
    div = entry.get("div")
    _check_guide_unchanged(qmd, _guide_lines(qmd), first, first_line, div)

    video = _video_for(manifest, {})
    note = _render(video, spec, new_image)
    # The render took seconds; read the guide again now and re-check before touching it.
    lines = _guide_lines(qmd)
    _check_guide_unchanged(qmd, lines, first, first_line, div)
    rel = os.path.relpath(new_image, qmd.parent).replace(os.sep, "/")
    alt = " ".join(caption.split()).replace("[", "(").replace("]", ")")   # one line, no bracket to close the alt
    new_line = f'![{alt}]({rel}){{fig-alt="{alt.replace(chr(34), chr(39))}"}}'

    eol = "\r" if lines[first_line].endswith("\r") else ""     # a CRLF guide stays CRLF
    if div is None:
        fragment = []
        if first_line > 0 and lines[first_line - 1].strip() != "":
            fragment.append(eol)
        fragment += ["::: {layout-ncol=2}" + eol, lines[first_line], eol, new_line + eol, ":::" + eol]
        if first_line + 1 < len(lines) and lines[first_line + 1].strip() != "":
            fragment.append(eol)
        lines[first_line:first_line + 1] = fragment
    else:
        open_line, close_line = div
        new_n = len(shots) + 1
        lines[open_line] = LAYOUT_NCOL_RE.sub(lambda mo: f"{mo.group(1)}{new_n}", lines[open_line], count=1)
        lines[close_line:close_line] = [eol, new_line + eol]

    _replace_text(qmd, "\n".join(lines))
    return new_image, f"added {new_image.name}" + note


def _removed_paths(removed_dir: Path, srcs: list[Path]) -> list[Path]:
    """Destinations inside `removed_dir` keeping each name; on a collision the same `-N`
    goes on every file of the bundle, so an image and its sidecar keep one stem."""
    n = 0
    while True:
        tag = f"-{n}" if n else ""
        dests = [removed_dir / f"{src.stem}{tag}{src.suffix}" for src in srcs]
        if not any(d.exists() for d in dests):
            return dests
        n += 1


def remove_image(manifest: dict, step: int, decision: dict) -> tuple[Path, str]:
    """Remove one shot from a multi-shot step: splice the guide, byte-exact, reversing what
    `add_image` inserted, and move the image (and its sidecar / `.check` twin, whichever
    exist) into `<out_dir>/removed/` -- never deletes a file. Refuses a one-shot step (there
    is no div to unwrap) and an out-of-range shot index."""
    entry = manifest["steps"].get(str(step)) or manifest["steps"].get(step)
    if not entry:
        raise VeditError(f"step {step} is not in the review manifest")
    shots = entry["shots"]
    div = entry.get("div")
    if len(shots) < 2 or div is None:
        raise VeditError(f"step {step} has only one image; remove it by editing the guide")
    shot_index = decision.get("shot", 0)
    if not 0 <= shot_index < len(shots):
        raise VeditError(f"step {step} has no shot index {shot_index}")
    shot = shots[shot_index]
    image = Path(shot["image"])
    line = shot["line"]
    open_line, close_line = div
    named = decision.get("image")
    if named is not None and Path(str(named)).name != image.name:    # a stale page's index
        raise VeditError(f"shot {shot_index} of step {step} is now {image.name}, not "
                         f"{Path(str(named)).name}; reload the page")

    qmd = Path(manifest["guide"])
    root = qmd.parent.resolve()
    if not _inside(image.resolve(), root):
        raise VeditError(f"{image.name} lies outside the guide folder; remove it by editing the guide")
    lines = _guide_lines(qmd)
    _check_guide_unchanged(qmd, lines, image, line, div)
    live = sum(1 for j in range(open_line + 1, close_line) if IMAGE_RE.match(lines[j]))
    if live != len(shots):
        raise VeditError(f"{qmd.name} has changed since the review page was built (its layout div "
                         f"now holds {live} images, the page knew {len(shots)}); rebuild the page")

    # Reverse the ["", new_line] (or [new_line, ""]) pair `add_image` inserted: delete the
    # image line itself, plus whichever neighbour blank line is still inside the div -- the
    # one right before it if there is one, else the one right after.
    before_idx, after_idx = line - 1, line + 1
    to_delete = [line]
    if before_idx > open_line and lines[before_idx].strip() == "":
        to_delete.append(before_idx)
    elif after_idx < close_line and lines[after_idx].strip() == "":
        to_delete.append(after_idx)
    for idx in sorted(to_delete, reverse=True):
        del lines[idx]
    close_line -= len(to_delete)

    remaining = len(shots) - 1
    if remaining == 1:                # nothing left to lay out side by side: unwrap the div
        del lines[close_line]
        del lines[open_line]
    else:
        lines[open_line] = LAYOUT_NCOL_RE.sub(lambda mo: f"{mo.group(1)}{remaining}", lines[open_line], count=1)

    _replace_text(qmd, "\n".join(lines))

    removed_dir = Path(manifest["out_dir"]) / "removed"
    removed_dir.mkdir(parents=True, exist_ok=True)
    bundle = [src for src in (image, image.with_suffix(".json"),
                              image.with_name(f"{image.stem}.check{image.suffix}")) if src.exists()]
    moved_image = None
    for src, dest in zip(bundle, _removed_paths(removed_dir, bundle)):
        shutil.move(str(src), str(dest))
        if src == image:
            moved_image = dest
    return moved_image, f"removed {image.name} (moved to review/removed/)"


APPLIED_ACTIONS = {"moment", "box", "crop", "edit"}


def apply_all(out_dir: Path) -> list[str]:
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    review_path = out_dir / "review.json"
    if not review_path.exists():
        raise VeditError(f"{review_path} does not exist; save the page's JSON there first")
    log = json.loads(review_path.read_text(encoding="utf-8"))
    manifest.setdefault("out_dir", str(out_dir.resolve()))    # manifests built before the key existed
    lines = []
    for step, decisions in sorted(log.items(), key=lambda kv: int(kv[0])):
        for decision in decisions:
            action = decision.get("action")
            if decision.get("error"):
                lines.append(f"step {step}: skipped {action} (refused when made: {decision['error']})")
            elif action in APPLIED_ACTIONS:
                image, message = apply_decision(manifest, int(step), decision)
                lines.append(f"step {step}: {message} -> {image.name}")
            elif action in ("add", "remove"):
                fn = add_image if action == "add" else remove_image
                image, message = fn(manifest, int(step), decision)
                lines.append(f"step {step}: {message}")
                # The guide's line numbers and the step's shot list moved: rebuild before the
                # next decision so a second add/remove lands correctly, not over the last one.
                build(Path(manifest["guide"]), out_dir, video=Path(manifest["video"]),
                      width=manifest.get("page_width", DEFAULT_WIDTH), serve=manifest.get("serve", False))
                manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            elif decision.get("note"):
                lines.append(f"step {step}: note only")
    return lines


# ---- serving ------------------------------------------------------------------------------

def _inside(target: Path, *roots: Path) -> bool:
    """Containment by path parts, so `/x/guide-review` is not inside `/x/guide`."""
    return any(target == r or target.is_relative_to(r) for r in roots)


def _write_log(path: Path, log: dict) -> None:
    _replace_text(path, json.dumps(log, indent=2) + "\n")


def serve(out_dir: Path, port: int, *, quiet: bool = False) -> ThreadingHTTPServer:
    """Serve the guide folder (so the page's relative image paths resolve) and apply
    decisions POSTed to /decide. Returns the running server; call .shutdown() to stop."""
    out_dir = out_dir.resolve()
    manifest_path = out_dir / "manifest.json"
    state = {"manifest": json.loads(manifest_path.read_text(encoding="utf-8"))}
    state["manifest"].setdefault("out_dir", str(out_dir))
    root = Path(state["manifest"]["guide"]).parent.resolve()
    video = Path(state["manifest"]["video"])
    duration = probe(video).duration
    review_path = out_dir / "review.json"
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: N802
            if not quiet:
                sys.stderr.write("review  " + (args[0] % args[1:]) + "\n")

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _bytes(self, data: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)

            if path == "/frame":
                raw = (parse_qs(parsed.query).get("at") or [None])[0]
                try:
                    at = float(raw)
                except (TypeError, ValueError):
                    self.send_error(400)
                    return
                if not (0 <= at < duration):
                    self.send_error(404)
                    return
                try:
                    frame = frame_path(out_dir, video, at, state["manifest"]["page_width"])
                except VeditError:
                    self.send_error(404)
                    return
                self._bytes(frame.read_bytes(), "image/jpeg")
                return

            if path == "/review.json":
                if not review_path.exists():
                    self.send_error(404)
                    return
                self._bytes(review_path.read_bytes(), "application/json")
                return

            if path in ("", "/"):
                target = out_dir / "review.html"
            else:
                # The page's image paths are relative to the review folder ("../g-guide-img/x.png");
                # a browser folds the "../" away against "/", so try both bases. Anything that
                # resolves outside the guide folder is refused.
                rel = path.lstrip("/")
                target = next((t for t in ((out_dir / rel).resolve(), (root / rel).resolve())
                               if _inside(t, out_dir, root) and t.is_file()), None)
            if target is None or not _inside(target, out_dir, root) or not target.is_file():
                self.send_error(404)
                return
            data = target.read_bytes()
            ctype = {".html": "text/html; charset=utf-8", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".png": "image/png", ".json": "application/json"}.get(target.suffix.lower(),
                                                                          "application/octet-stream")
            self._bytes(data, ctype)

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/decide":
                self.send_error(404)
                return
            length = int(self.headers.get("content-length", "0"))
            try:
                decision = json.loads(self.rfile.read(length) or b"{}")
                step = int(decision.pop("step"))
            except (ValueError, KeyError, TypeError):
                self._json(400, {"ok": False, "error": "bad decision"})
                return
            with lock:
                log = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
                log.setdefault(str(step), []).append(decision)
                manifest = state["manifest"]
                action = decision.get("action")
                try:
                    if action in ("add", "remove"):
                        fn = add_image if action == "add" else remove_image
                        new_image, message = fn(manifest, step, decision)
                        try:
                            build(Path(manifest["guide"]), out_dir, video=Path(manifest["video"]),
                                  width=manifest["page_width"], serve=manifest["serve"])
                        except VeditError as exc:
                            raise VeditError(f"{message}, but the page could not be rebuilt: {exc}; "
                                             "stop the server and run vedit review again") from exc
                        state["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
                    else:
                        image, message = apply_decision(manifest, step, decision)
                except VeditError as exc:
                    decision["error"] = str(exc)       # kept in the log, skipped by --apply
                    _write_log(review_path, log)
                    self._json(200, {"ok": False, "error": str(exc)})
                    return
                _write_log(review_path, log)
                if action in ("add", "remove"):
                    self._json(200, {"ok": True, "message": message, "reload": True})
                    return
                shot = decision.get("shot", 0)
                rel = os.path.relpath(image, out_dir).replace(os.sep, "/")
                reply = {"ok": True, "message": message, "step": step, "shot": shot, "image": rel}
                if decision.get("action") == "edit":
                    sidecar = image.with_suffix(".json")
                    if sidecar.exists():          # read back under the lock: it is this request's result
                        result_spec = json.loads(sidecar.read_text(encoding="utf-8"))
                        reply["at"] = result_spec.get("at")
                        reply["boxes"] = _shot_boxes(result_spec)
                        reply["crop"] = result_spec.get("crop")
            self._json(200, reply)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
