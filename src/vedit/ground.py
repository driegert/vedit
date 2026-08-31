"""Deterministic grounding for `vedit still`: does each highlight's box cover the text
its label names?

A vision model asked "is the box on the right thing?" answers yes whenever the box is in
the right *neighbourhood* — it cannot see a first word clipped off or a box hugging the
whitespace beside a link. Screenshots are flat synthetic images, so that question has a
deterministic answer: OCR the frame (tesseract, word boxes as TSV), find the label's text
on screen, and measure how much of it the drawn box covers.

Tolerances are deliberate. A mouse cursor over a menu turns "Document" into "Docurt" at
confidence 0, so tokens match on prefix/similarity, a line qualifies with half its tokens,
and the phrase box is built from the words that did match. The check never fails a render:
its findings go to stderr, and the caller (usually a model) decides. `VEDIT_NO_OCR=1`
disables it; a missing `tesseract` binary skips it with one note.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .media import MediaInfo, ffmpeg, run
from .still import Highlight, Rect, StillSpec

COVERED = 0.85          # fraction of the found phrase box that must lie inside the drawn box
LINE_SCORE = 0.5        # fraction of query tokens a line must match to be a candidate
MIN_QUERY_CHARS = 3
PSM = "11"              # sparse text: UI labels, menus, buttons

# Instruction words a label may lead with; what follows is the on-screen text.
_VERBS = ("click", "double-click", "right-click", "pick", "select", "choose", "check", "uncheck",
          "tick", "untick", "press", "open", "tap", "hit", "enable", "disable", "expand", "run")
_LEAD = re.compile(r"^\s*(?:\d+[.)]\s*)?(?:(?:%s)\b\s*:?\s*)?(?:on\s+|the\s+)?" % "|".join(_VERBS),
                   re.IGNORECASE)
_QUOTED = re.compile(r"[\"“”']([^\"“”']{3,})[\"“”']")
_SPLIT = re.compile(r"\s+[-–—]\s+|:\s+")


@dataclass(frozen=True)
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float

    @property
    def token(self) -> str:
        return _norm(self.text)


@dataclass(frozen=True)
class Finding:
    index: int
    label: str
    query: str | None
    found: Rect | None      # box of the matched words on screen (best candidate)
    matched: str | None     # the OCR text that matched
    coverage: float | None  # fraction of `found` inside the drawn box
    ok: bool | None         # None = could not check
    count: int = 0          # how many places on screen matched equally well


def available() -> bool:
    return os.environ.get("VEDIT_NO_OCR", "") not in ("1", "true", "yes") \
        and shutil.which("tesseract") is not None


def _norm(text: str) -> str:
    return re.sub(r"^[^\w.]+|[^\w.]+$", "", text.lower()).strip(".")


def queries_for(label: str) -> list[str]:
    """Candidate on-screen strings named by a label, most specific first."""
    quoted = [q.strip() for q in _QUOTED.findall(label)]
    if quoted:
        return quoted
    stripped = _LEAD.sub("", label).strip().rstrip(".:;,!")
    out = []
    if len(stripped) >= MIN_QUERY_CHARS:
        out.append(stripped)
    for part in _SPLIT.split(stripped):
        part = _LEAD.sub("", part).strip().rstrip(".:;,!")
        if len(part) >= MIN_QUERY_CHARS and part not in out:
            out.append(part)
    return out


def _tokens(text: str) -> list[str]:
    return [t for t in (_norm(p) for p in text.split()) if t]


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    shared = len(os.path.commonprefix([a, b]))
    if shared >= max(4, 0.6 * min(len(a), len(b))):      # docurt ~ document; not download ~ downtown
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.75


def extract_frame(info: MediaInfo, spec: StillSpec, out: Path) -> Path:
    if info.is_image:
        return info.path
    cmd = [ffmpeg(), "-y", "-v", "error"]
    if spec.frame is not None:
        cmd += ["-ss", f"{max(0.0, (spec.frame - 0.5) / float(info.fps)):.6f}"]
    cmd += ["-i", str(info.path), "-frames:v", "1", "-update", "1", str(out)]
    run(cmd, what="extracting the frame for OCR")
    return out


def ocr(image: Path) -> list[Word]:
    proc = subprocess.run(["tesseract", str(image), "stdout", "--psm", PSM, "tsv"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    words = []
    for row in proc.stdout.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) != 12 or not cols[11].strip():
            continue
        try:
            x, y, w, h, conf = int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]), float(cols[10])
        except ValueError:
            continue
        if conf < 0 or w <= 0 or h <= 0:
            continue
        words.append(Word(cols[11], x, y, w, h, conf))
    return words


def lines(words: list[Word]) -> list[list[Word]]:
    """Group words into visual lines by vertical overlap, independent of tesseract's blocks."""
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.y, w.x)):
        for row in rows:
            ref = row[-1]
            overlap = min(ref.y + ref.h, w.y + w.h) - max(ref.y, w.y)
            if overlap >= 0.5 * min(ref.h, w.h) and w.x - (ref.x + ref.w) < 4 * max(ref.h, w.h):
                row.append(w)
                break
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w.x) for r in rows]


def _bbox(words: list[Word]) -> Rect:
    x0 = min(w.x for w in words)
    y0 = min(w.y for w in words)
    x1 = max(w.x + w.w for w in words)
    y1 = max(w.y + w.h for w in words)
    return Rect(x0, y0, x1 - x0, y1 - y0)


def find(query: str, rows: list[list[Word]]) -> list[tuple[float, Rect, str]]:
    """Lines matching `query`: (score, box of the matched words, their text), best first."""
    q = _tokens(query)
    if not q:
        return []
    hits = []
    for row in rows:
        best = None
        for start in range(len(row)):
            matched, qi = [], 0
            for w in row[start:]:
                if qi < len(q) and _token_match(w.token, q[qi]):
                    matched.append(w)
                    qi += 1
                elif matched and qi < len(q) and w.conf == 0:
                    # a garbled word (cursor, anti-aliasing) may stand in for the next token
                    matched.append(w)
                    qi += 1
                elif matched:
                    break
                if qi == len(q):
                    break
            score = sum(1 for w in matched if w.conf > 0) / len(q)
            if matched and (best is None or score > best[0]):
                best = (score, matched)
        if best and best[0] >= LINE_SCORE:
            hits.append((best[0], _bbox(best[1]), " ".join(w.text for w in best[1])))
    hits.sort(key=lambda h: -h[0])
    return hits


def _coverage(found: Rect, drawn: Rect) -> float:
    ix = max(0, min(found.right, drawn.right) - max(found.x, drawn.x))
    iy = max(0, min(found.bottom, drawn.bottom) - max(found.y, drawn.y))
    area = found.w * found.h
    return (ix * iy) / area if area else 0.0


def _generic(query: str) -> bool:
    """One short word ("Next", "OK", "Run") recurs all over a screen: finding it far from
    the box says nothing about the box."""
    toks = _tokens(query)
    return len(toks) == 1 and len(toks[0]) <= 4


def region_words(frame: Path, h: Highlight, info: MediaInfo, workdir: Path) -> list[Word]:
    """OCR the neighbourhood of the drawn box at 3x: button and menu text is small, and
    tesseract's full-frame pass drops much of it."""
    margin = max(120, h.rect.w, h.rect.h)
    x0, y0 = max(0, h.rect.x - margin), max(0, h.rect.y - margin)
    x1, y1 = min(info.width, h.rect.right + margin), min(info.height, h.rect.bottom + margin)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return []
    crop = workdir / f"region-{h.rect.x}-{h.rect.y}.png"
    run([ffmpeg(), "-y", "-v", "error", "-i", str(frame),
         "-vf", f"crop={x1 - x0}:{y1 - y0}:{x0}:{y0},scale=iw*3:ih*3:flags=lanczos", str(crop)],
        what="cropping the highlight region for OCR")
    return [Word(w.text, x0 + w.x // 3, y0 + w.y // 3, max(1, w.w // 3), max(1, w.h // 3), w.conf)
            for w in ocr(crop)]


def check_highlight(index: int, h: Highlight, near: list[list[Word]],
                    far: list[list[Word]]) -> Finding:
    label = h.label or ""
    for query in queries_for(label):
        # Near pass first: found inside the drawn box settles it.
        hits = find(query, near)
        covered = [(c, r, t) for s, r, t in hits for c in [_coverage(r, h.rect)] if c >= COVERED]
        if covered:
            c, r, t = max(covered, key=lambda x: x[0])
            # The same text elsewhere on screen (a second "Windows 11" row) is worth a word.
            elsewhere = [r2 for s2, r2, _ in find(query, far)
                         if _coverage(r2, h.rect) < COVERED and s2 >= hits[0][0] - 1e-9]
            return Finding(index, label, query, r, t, c, True, 1 + len(elsewhere))
        # Otherwise the whole frame says where the text really is.
        hits = find(query, far) or hits
        if not hits:
            continue
        top = hits[0][0]
        peers = [(s, r, t) for s, r, t in hits if s >= top - 1e-9]
        covered = [(c, r, t) for s, r, t in peers for c in [_coverage(r, h.rect)] if c >= COVERED]
        if covered:
            c, r, t = max(covered, key=lambda x: x[0])
            return Finding(index, label, query, r, t, c, True, len(peers))
        s, r, t = peers[0]
        c = max(_coverage(r2, h.rect) for _, r2, _ in peers)
        if c < 0.05 and _generic(query):
            return Finding(index, label, query, None, t, None, None, len(peers))
        best = max(peers, key=lambda p: _coverage(p[1], h.rect))
        return Finding(index, label, query, best[1], best[2], c, False, len(peers))
    return Finding(index, label, None, None, None, None, None, 0)


def check(spec: StillSpec, info: MediaInfo) -> list[Finding]:
    labelled = [(i, h) for i, h in enumerate(spec.highlights) if h.label]
    if not labelled:
        return []
    with tempfile.TemporaryDirectory(prefix="vedit-ocr-") as workdir:
        wd = Path(workdir)
        frame = extract_frame(info, spec, wd / "frame.png")
        far: list[list[Word]] | None = None
        out = []
        for i, h in labelled:
            near = lines(region_words(frame, h, info, wd))
            if far is None:
                far = lines(ocr(frame))
            out.append(check_highlight(i, h, near, far))
    return out


def report(findings: list[Finding]) -> list[str]:
    out = []
    for f in findings:
        tag = f"grounding  highlight[{f.index}] {f.label!r}:"
        times = f" (that text appears {f.count}x on screen — confirm it is the right one)" \
            if f.count > 1 else ""
        if f.ok is None and f.query is None:
            out.append(f"{tag} no matching text found on screen — verify by eye in the .check twin")
        elif f.ok is None:
            out.append(f"{tag} {f.matched!r} is not inside or near the box (only elsewhere on "
                       f"screen) — a short word proves nothing; verify by eye in the .check twin")
        elif f.ok:
            out.append(f"{tag} OCR finds {f.matched!r} at x {f.found.x} y {f.found.y} "
                       f"w {f.found.w} h {f.found.h} — covered by the box{times}")
        else:
            verdict = "LIKELY MISS" if f.coverage < 0.5 else "CLIPS the text"
            out.append(f"{tag} OCR finds {f.matched!r} at x {f.found.x} y {f.found.y} "
                       f"w {f.found.w} h {f.found.h} but the drawn box covers only "
                       f"{f.coverage:.0%} of it. {verdict} — set x {f.found.x} y {f.found.y} "
                       f"(w {f.found.w} h {f.found.h}) and re-run, then read the .check twin")
    return out
