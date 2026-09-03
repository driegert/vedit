"""OCR for `vedit still`: where is a piece of text on a frame?

The model driving `still` is good at naming the control a screenshot should point at and
bad at measuring where it is — every layer that asked a model for pixels (blind guesses,
a reviewer's "correct x=…" hints) produced numbers off by hundreds of pixels that were
then applied verbatim. Screenshots are flat synthetic images, so for anything that *is*
text the question has a deterministic answer: OCR the frame and look the words up.

Tesseract reads a 1080p frame badly at 1x (button and menu text is too small), so the
frame is cut into overlapping tiles scaled 3x and the word boxes are mapped back to
full-frame pixels. It still cannot read light-on-dark button captions or a dark terminal
reliably — those stay on the gridded-frame path in `still`, and `locate()` says so when
it fails.

Matching is deliberately strict where it matters: a token that is short or carries a
digit ("4.5", "x64", "11", ".exe") must match exactly, because that is the token that
tells two rows apart; only longer alphabetic tokens tolerate a cursor-garbled character.
`VEDIT_NO_OCR=1` disables everything here; a missing `tesseract` binary does the same
with one note.
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

from . import VeditError
from .geometry import Rect
from .media import MediaInfo, ffmpeg, run

PSM = "11"              # sparse text: UI labels, menus, buttons
SCALE = 3
# Tiles overlap by 320 px, so any word up to that wide sits whole inside some tile, well
# away from an edge (tesseract reads the first and last 20 px of a tile badly).
TILE_W, TILE_H, OVERLAP = 960, 540, 320
FUZZY_MIN = 5           # tokens shorter than this, or carrying a digit, match exactly only
LINE_SCORE = 0.5        # fraction of query tokens a line must match to count as a partial hit
MAX_JOIN = 3            # OCR may split one token ("RStudio-2026" ".08.2-200.exe"); rejoin up to this many
CLOSEST = 5             # lines listed when a text anchor is not found

# Instruction words a label may lead with; what follows is the on-screen text.
_VERBS = ("click", "double-click", "right-click", "pick", "select", "choose", "check", "uncheck",
          "tick", "untick", "press", "open", "tap", "hit", "enable", "disable", "expand", "run")
_LEAD = re.compile(r"^\s*(?:\d+[.)]\s*)?(?:(?:%s)\b\s*:?\s*)?(?:on\s+|the\s+)?" % "|".join(_VERBS),
                   re.IGNORECASE)
_QUOTED = re.compile(r"[\"“”']([^\"“”']{3,})[\"“”']")
_SPLIT = re.compile(r"\s+[-–—]\s+|:\s+")
_DIGIT_LOOKALIKES = str.maketrans({"l": "1", "I": "1", "O": "0", "o": "0", "@": "0"})


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

    @property
    def rect(self) -> Rect:
        return Rect(self.x, self.y, self.w, self.h)


@dataclass(frozen=True)
class Hit:
    score: float            # fraction of the query's tokens matched, in order
    rect: Rect              # box of the matched words
    text: str               # the OCR text that matched
    line: str               # the whole OCR line it sits on


def available() -> bool:
    return os.environ.get("VEDIT_NO_OCR", "") not in ("1", "true", "yes") \
        and shutil.which("tesseract") is not None


# ---- tokens --------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"^[^\w.]+|[^\w.]+$", "", text.lower()).strip(".")


def _tokens(text: str) -> list[str]:
    return [t for t in (_norm(p) for p in text.split()) if t]


def _discriminating(token: str) -> bool:
    """Short tokens and anything with a digit are what tell two rows apart ("4.5" vs
    "4.4", "gear" vs "Sear"): they never fuzzy-match."""
    return len(token) < FUZZY_MIN or _numeric(token)


def _numeric(token: str) -> bool:
    """A digit-bearing token is the one a candidate may not do without: a line reading
    "RTools 4.4" is not a partial hit for "RTools 4.5". A dropped short word ("you") is
    an ordinary OCR miss and only costs score."""
    return any(ch.isdigit() for ch in token)


def _digits(token: str) -> str:
    """A digit-bearing token with OCR's usual look-alikes folded (l/I -> 1, O/o -> 0)."""
    return token.translate(_DIGIT_LOOKALIKES) if any(ch.isdigit() for ch in token) else token


def _squash(text: str) -> str:
    return re.sub(r"[.\-_\s]", "", _digits(text.lower()))


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if _discriminating(a) or _discriminating(b):
        return _digits(a) == _digits(b)
    shared = len(os.path.commonprefix([a, b]))
    if shared >= max(4, 0.6 * min(len(a), len(b))):      # docurt ~ document; not download ~ downtown
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.75


def _generic(query: str) -> bool:
    """One short word ("Next", "OK", "Run") recurs all over a screen: finding it far from
    the box says nothing about the box."""
    toks = _tokens(query)
    return len(toks) == 1 and len(toks[0]) <= 4


def queries_for(label: str) -> list[str]:
    """Candidate on-screen strings named by a label, most specific first."""
    quoted = [q.strip() for q in _QUOTED.findall(label)]
    if quoted:
        return quoted
    stripped = _LEAD.sub("", label).strip().rstrip(".:;,!")
    out = []
    if len(stripped) >= 3:
        out.append(stripped)
    for part in _SPLIT.split(stripped):
        part = _LEAD.sub("", part).strip().rstrip(".:;,!")
        if len(part) >= 3 and part not in out:
            out.append(part)
    return out


# ---- reading the frame -----------------------------------------------------------------

def extract_frame(info: MediaInfo, frame: int | None, out: Path) -> Path:
    if info.is_image:
        return info.path
    cmd = [ffmpeg(), "-y", "-v", "error"]
    if frame is not None:
        cmd += ["-ss", f"{max(0.0, (frame - 0.5) / float(info.fps)):.6f}"]
    cmd += ["-i", str(info.path), "-frames:v", "1", "-update", "1", str(out)]
    run(cmd, what="extracting the frame for OCR")
    return out


def ocr_image(image: Path) -> list[Word]:
    """Tesseract word boxes for one image, as-is (no scaling, no offset)."""
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


def tiled(frame: Path, width: int, height: int, workdir: Path) -> list[Word]:
    """OCR the frame as overlapping tiles scaled 3x; boxes come back in frame pixels."""
    words: list[Word] = []
    tile = workdir / "tile.png"
    for ty in range(0, height, TILE_H - OVERLAP):
        for tx in range(0, width, TILE_W - OVERLAP):
            w, h = min(TILE_W, width - tx), min(TILE_H, height - ty)
            if w < 8 or h < 8:
                continue
            run([ffmpeg(), "-y", "-v", "error", "-i", str(frame),
                 "-vf", f"crop={w}:{h}:{tx}:{ty},scale=iw*{SCALE}:ih*{SCALE}:flags=lanczos", str(tile)],
                what="cropping a tile for OCR")
            for wd in ocr_image(tile):
                words.append(Word(wd.text, tx + wd.x // SCALE, ty + wd.y // SCALE,
                                  max(1, wd.w // SCALE), max(1, wd.h // SCALE), wd.conf))
            if tx + w >= width:
                break
        if ty + h >= height:
            break
    return _dedupe(words)


def _dedupe(words: list[Word]) -> list[Word]:
    """A word read twice (two overlapping tiles, or a tile and the 1x pass) keeps one reading.

    The same box read twice keeps the more confident text — judged on the boxes alone,
    since the second reading of the same pixels is often garbled ("KLOOIS" beside
    "RTools"). A box nested inside a bigger one is dropped only when its text is a
    fragment of the bigger reading ("RStudio-2026" inside "RStudio-2026.08.2-200.exe",
    cut off at a tile edge); otherwise both stay, and the matcher sorts it out."""
    # Specks: a 6x1 "=", a 5x2 "pS", a 3x5 ">" — not text at any resolution this runs at.
    words = [w for w in words if w.h >= MIN_GLYPH_H and w.w >= MIN_GLYPH_W]
    kept: list[Word] = []
    for w in sorted(words, key=lambda w: (-w.conf, w.w * w.h)):
        r = w.rect
        if any(r.overlap(k.rect) > 0.7 and k.rect.overlap(r) > 0.7 for k in kept):
            continue
        kept.append(w)
    # Nested boxes, smaller inside bigger: a bigger box more than twice as tall as a
    # confident reading inside it spans two lines (a tall junk "a") and goes; a smaller
    # box whose text is a fragment of the bigger's reading ("RStudio-2026" inside the full
    # filename, cut off at a tile edge) goes. Anything else nested stays — junk read off
    # a real word's pixels ("POD" inside "For") is harmless once `lines` refuses to let
    # it start a line — because every rule that guessed at "the less confident one is
    # junk" deleted real words somewhere on the fixture frames.
    dropped: set[int] = set()
    for i, w in enumerate(kept):
        for j, k in enumerate(kept):
            if i == j or not _nested(w, k):
                continue
            if k.h > 2 * w.h and w.conf >= 50 and w.w >= 2 * w.h:     # a word, not a lone glyph
                dropped.add(j)
            elif _squash(w.text) and _squash(w.text) in _squash(k.text):
                dropped.add(i)
    return [w for i, w in enumerate(kept) if i not in dropped]


def _nested(w: Word, k: Word) -> bool:
    """`w` sits inside the bigger box `k`."""
    return k.w * k.h > w.w * w.h and w.rect.overlap(k.rect) > 0.7


def lines(words: list[Word]) -> list[list[Word]]:
    """Group words into visual lines by vertical overlap, independent of tesseract's blocks.

    Words are taken left to right and each joins the row whose right end it overlaps
    vertically and sits within a few letter-heights of. (Taking them top to bottom
    instead split one line into two whenever its words' baselines differed by a pixel.)
    A word nested inside a bigger one — junk read off a real word's pixels — may join a
    line but never start one, or it would start a line of its own and pull the next real
    word onto it.
    """
    nested = {id(w) for w in words if any(_nested(w, k) for k in words)}
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (id(w) in nested, w.x, w.y)):
        best, best_fit = None, 0.0
        for row in rows:
            ref = row[-1]
            overlap = min(ref.y + ref.h, w.y + w.h) - max(ref.y, w.y)
            fit = overlap / max(ref.h, w.h)      # a tall junk reading fits a line worse than a peer
            if overlap >= 0.5 * min(ref.h, w.h) and -2 <= w.x - (ref.x + ref.w) < 4 * max(ref.h, w.h) \
                    and fit > best_fit:
                best, best_fit = row, fit
        if best is None:
            rows.append([w])
        else:
            best.append(w)
    return sorted(rows, key=lambda r: (min(w.y for w in r), r[0].x))


def read(image: Path, width: int, height: int, workdir: Path) -> list[Word]:
    """Every word on the frame, from three passes merged: tiled 3x (small UI text), the
    same on the inverted frame (a dark-theme console or terminal reads as light text on
    dark, which tesseract mostly drops), and a plain 1x pass (which reads some things
    the upscale spoils, like underlined links). About 20 s for a 1080p frame."""
    inverted = workdir / "inverted.png"
    run([ffmpeg(), "-y", "-v", "error", "-i", str(image), "-vf", "negate", str(inverted)],
        what="inverting the frame for OCR")
    return _dedupe(tiled(image, width, height, workdir)
                   + tiled(inverted, width, height, workdir)
                   + ocr_image(image))


_cache: dict[tuple[str, int | None], list[list[Word]]] = {}


def rows_for(info: MediaInfo, frame: int | None) -> list[list[Word]]:
    """The frame's OCR lines, read once per process: `still` resolves text anchors and the
    grounding check both ask, and one tiled pass is ~12 s."""
    key = (str(info.path.resolve()), None if info.is_image else frame)
    if key not in _cache:
        with tempfile.TemporaryDirectory(prefix="vedit-ocr-") as workdir:
            wd = Path(workdir)
            image = extract_frame(info, frame, wd / "frame.png")
            _cache[key] = lines(read(image, info.width, info.height, wd))
    return _cache[key]


# ---- matching ---------------------------------------------------------------------------

def _bbox(words: list[Word]) -> Rect:
    x0 = min(w.x for w in words)
    y0 = min(w.y for w in words)
    x1 = max(w.x + w.w for w in words)
    y1 = max(w.y + w.h for w in words)
    return Rect(x0, y0, x1 - x0, y1 - y0)


MAX_SKIP = 2            # junk words OCR may insert inside a phrase (a checkbox glyph, a cursor)
STRICT_SCORE = 0.75     # `locate` accepts a line missing one short word of four ("you")
FUZZY_CREDIT = 0.9      # a near-match token ("installed" for "installer") scores below an exact one
MIN_GLYPH_H, MIN_GLYPH_W = 7, 4     # px; anything smaller is a speck, not a character
GLUE_MIN = 8            # a token at least this long may be matched by a word that merely starts with it


def _match_from(row: list[Word], start: int, q: list[str]) -> tuple[list[Word], int] | None:
    """Match the query tokens in order from `row[start]`.

    Returns (matched words, tokens matched), or None when a digit-bearing token could not
    be matched — that candidate is void: a line reading "RTools 4.4" is not a partial hit
    for "RTools 4.5". Tolerances: a token OCR split in two is rejoined; a conf-0 word may
    stand in for a long token; up to MAX_SKIP junk words between two matches are skipped.
    """
    matched: list[Word] = []
    qi, i, credited = 0, start, 0.0
    while i < len(row) and qi < len(q):
        w, tok = row[i], q[qi]
        # Exact is exact whatever tesseract's confidence: it scores a word it does not
        # know ("install.packages") at 0 even when every character is right. A word that
        # merely *starts* with a long token is the token with following characters glued
        # on (a bracket, a quote); a word the token starts with is a truncated reading and
        # is never exact — that is how "RStudio-2026" once passed for the .exe.
        exact = (w.token == tok or _digits(w.token) == _digits(tok)
                 or (len(tok) >= GLUE_MIN and not _numeric(tok) and w.token.startswith(tok)))
        if exact or _token_match(w.token, tok):
            matched.append(w)
            if exact:
                credited += 1.0
            elif w.conf > 0:
                credited += FUZZY_CREDIT                                # near ranks below exact
            qi += 1
            i += 1
            continue
        joined = None
        for k in range(2, MAX_JOIN + 1):
            piece = row[i:i + k]
            if len(piece) == k and _squash(tok) and _squash("".join(p.text for p in piece)) == _squash(tok):
                joined = piece
                break
        if joined:
            matched += joined
            credited += 1
            qi += 1
            i += len(joined)
            continue
        if matched and w.conf == 0 and not _discriminating(tok):
            # a garbled word (cursor, anti-aliasing) may stand in for a long token
            matched.append(w)
            qi += 1
            i += 1
            continue
        skip = next((k for k in range(1, MAX_SKIP + 1)
                     if matched and i + k < len(row) and _token_match(row[i + k].token, tok)), None)
        if skip:
            i += skip
            continue
        break
    if not matched:
        return None
    if any(_numeric(t) for t in q[qi:]):
        return None
    return matched, credited


def find(query: str, rows: list[list[Word]], *, strict: bool = False) -> list[Hit]:
    """Lines matching `query`, best first (then top to bottom). `strict` keeps only lines
    matching (nearly) every token; a partial hit needs half of them."""
    q = _tokens(query)
    if not q:
        return []
    floor = (STRICT_SCORE if strict else LINE_SCORE) - 1e-9
    hits = []
    for row in rows:
        line = " ".join(w.text for w in row)
        # Every occurrence on the line, left to right, without overlapping: a sentence
        # naming "Rtools45 installer" twice is two places the caller must choose between.
        start = 0
        while start < len(row):
            found = _match_from(row, start, q)
            if not found:
                start += 1
                continue
            matched, credited = found
            score = min(credited / len(q), 1.0)
            if score >= floor:
                hits.append(Hit(score, _bbox(matched), " ".join(w.text for w in matched), line))
            start = row.index(matched[-1]) + 1
    hits.sort(key=lambda h: (-h.score, h.rect.y, h.rect.x))
    return hits


def closest(query: str, rows: list[list[Word]], n: int = CLOSEST) -> list[tuple[Rect, str]]:
    """The lines most similar to `query`, for a not-found message."""
    target = _squash(query)
    scored = []
    for row in rows:
        text = " ".join(w.text for w in row)
        ratio = SequenceMatcher(None, target, _squash(text)).ratio()
        if any(_norm(t) in _squash(text) for t in query.split() if len(t) >= 4):
            ratio += 0.25
        scored.append((ratio, _bbox(row), text))
    scored.sort(key=lambda s: -s[0])
    return [(r, t) for _, r, t in scored[:n]]


def describe(rect: Rect) -> str:
    return f"x {rect.x} y {rect.y} w {rect.w} h {rect.h}"


def locate(text: str, rows: list[list[Word]], *, near: tuple[int, int] | None = None,
           occurrence: int | None = None, where: str = "text") -> Hit:
    """The one place `text` is on screen — or an error that says exactly why not.

    Several places: the caller must say which, with `near` (frame pixels; the closest
    wins) or `occurrence` (1-based, top to bottom then left to right). None: the closest
    OCR lines are listed, and the message says when to fall back to measured x/y."""
    hits = find(text, rows, strict=True)
    if hits and hits[0].score >= 1 - 1e-9:
        hits = [h for h in hits if h.score >= 1 - 1e-9]     # exact readings outrank near ones
    if not hits:
        near_miss = closest(text, rows)
        listing = "\n".join(f"  {describe(r)}  {t}" for r, t in near_miss) or "  (nothing readable)"
        raise VeditError(
            f"{where}: {text!r} was not found on screen. The closest OCR lines are:\n{listing}\n"
            f"Name the text exactly as one of those lines shows it, or — for a button, an icon "
            f"or terminal text, which OCR often cannot read — give \"x\", \"y\", \"w\", \"h\" "
            f"measured on the gridded frame instead."
        )
    hits.sort(key=lambda h: (h.rect.y, h.rect.x))
    if len(hits) == 1:
        return hits[0]
    if occurrence is not None:
        if not 1 <= occurrence <= len(hits):
            raise VeditError(f"{where}: occurrence {occurrence} but {text!r} appears "
                             f"{len(hits)} times on screen")
        return hits[occurrence - 1]
    if near is not None:
        nx, ny = near
        return min(hits, key=lambda h: (h.rect.centre[0] - nx) ** 2 + (h.rect.centre[1] - ny) ** 2)
    listing = "\n".join(f"  #{i + 1}  {describe(h.rect)}  {h.line}" for i, h in enumerate(hits))
    raise VeditError(
        f"{where}: {text!r} appears {len(hits)} times on screen:\n{listing}\n"
        f"Say which one: add \"occurrence\": N (the # above), or \"near\": [x, y] (frame pixels; "
        f"the closest wins), or name a longer stretch of the line so only one matches."
    )


# ---- the text dump ------------------------------------------------------------------------

def dump(rows: list[list[Word]], pattern: str | None = None) -> list[str]:
    """One line per visual line of text, top to bottom, with full-frame pixel boxes."""
    regex = re.compile(pattern, re.IGNORECASE) if pattern else None
    out = []
    for row in sorted(rows, key=lambda r: (r[0].y, r[0].x)):
        text = " ".join(w.text for w in row)
        if regex and not regex.search(text):
            continue
        r = _bbox(row)
        out.append(f"x {r.x:4d} y {r.y:4d} w {r.w:4d} h {r.h:3d}  {text}")
    return out
