"""Grounding for `vedit still`: does a hand-placed box cover the text its label names?

Only boxes given as `x`/`y`/`w`/`h` are checked — a `text`-anchored highlight was placed
by OCR in the first place (see `ocr.locate`). The check is advisory: findings go to
stderr after the `wrote` lines, and a MISS tells the caller to *look* at the gridded
`.check` twin (or switch the highlight to a `text` anchor), never to move the box to a
number it has not seen. The render stands either way.

Rules that each closed a false verdict seen in practice:

- A hit inside the box counts only if nothing on the frame matches the label *better*.
  A half-score "RTools" under the box used to beat a full-score "RTools 4.5" two rows up.
- A MISS needs a full-score hit somewhere. A partial hit elsewhere ("restart" in a
  banner, when the button caption itself was unreadable) is "could not verify", not a
  miss pointing at the wrong place.
- Coverage is measured inside the outline, not against the padded rectangle: a box
  whose red line runs through the text does not pass.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ocr
from .geometry import Rect
from .media import MediaInfo
from .still import Highlight, StillSpec

COVERED = 0.85          # fraction of the found text box that must lie inside the outline
EDGE_GAP = 4            # pixels of clearance wanted between the outline and the text
EPS = 1e-9


@dataclass(frozen=True)
class Finding:
    index: int
    label: str
    query: str | None
    found: Rect | None      # box of the best matching text on screen
    matched: str | None     # the OCR text that matched
    coverage: float | None  # fraction of `found` inside the outline
    ok: bool | None         # None = could not check
    count: int = 0          # how many places matched equally well (full-score hits only)


def available() -> bool:
    return ocr.available()


def _inside(h: Highlight) -> Rect:
    return h.rect.inset(h.thickness + EDGE_GAP)


def check_highlight(index: int, h: Highlight, rows: list[list[ocr.Word]]) -> Finding:
    label = h.label or ""
    inside = _inside(h)
    partial: Finding | None = None
    for query in ocr.queries_for(label):
        hits = ocr.find(query, rows)
        if not hits:
            continue
        top = hits[0].score
        peers = [x for x in hits if x.score >= top - EPS]
        covered = [(x.rect.overlap(inside), x) for x in peers]
        covered = [(c, x) for c, x in covered if c >= COVERED]
        if covered:
            c, x = max(covered, key=lambda p: p[0])
            count = len(peers) if top >= 1 - EPS else 1
            return Finding(index, label, query, x.rect, x.text, c, True, count)
        if top < 1 - EPS:
            # Nothing on screen reads as the whole label: the best we can say is where a
            # fragment is. Remember it, keep trying the shorter queries.
            partial = partial or Finding(index, label, query, peers[0].rect, peers[0].text, None,
                                         None, len(peers))
            continue
        best_cov = max(x.rect.overlap(inside) for x in peers)
        if best_cov < 0.05 and ocr._generic(query):
            return Finding(index, label, query, None, peers[0].text, None, None, len(peers))
        best = max(peers, key=lambda x: x.rect.overlap(inside))
        return Finding(index, label, query, best.rect, best.text, best_cov, False, len(peers))
    if partial:
        return partial
    return Finding(index, label, None, None, None, None, None, 0)


def check_rows(spec: StillSpec, rows: list[list[ocr.Word]]) -> list[Finding]:
    return [check_highlight(i, h, rows) for i, h in enumerate(spec.highlights)
            if h.label and h.text is None]


def check(spec: StillSpec, info: MediaInfo) -> list[Finding]:
    if not any(h.label and h.text is None for h in spec.highlights):
        return []
    return check_rows(spec, ocr.rows_for(info, spec.frame))


def report(findings: list[Finding]) -> list[str]:
    out = []
    for f in findings:
        tag = f"grounding  highlight[{f.index}] {f.label!r}:"
        times = f" (that text appears {f.count}x on screen — confirm it is the right one)" \
            if f.count > 1 else ""
        if f.ok is None and f.query is None:
            out.append(f"{tag} no matching text found on screen — could not verify; "
                       f"read the .check twin")
        elif f.ok is None and f.coverage is None and f.found is not None:
            out.append(f"{tag} only a fragment matched ({f.matched!r} at {ocr.describe(f.found)}), "
                       f"not the whole label — could not verify; read the .check twin")
        elif f.ok is None:
            out.append(f"{tag} {f.matched!r} is found only elsewhere on screen — a short word "
                       f"proves nothing; read the .check twin")
        elif f.ok:
            out.append(f"{tag} OCR finds {f.matched!r} at {ocr.describe(f.found)} — inside the "
                       f"box{times}")
        else:
            verdict = "LIKELY MISS" if f.coverage < 0.3 else "CLIPS the text"
            out.append(f"{tag} {verdict} — OCR finds {f.matched!r} at {ocr.describe(f.found)}, "
                       f"and the outline encloses {f.coverage:.0%} of it. Either anchor this "
                       f"highlight with \"text\": {f.matched!r} instead of x/y, or read the "
                       f".check twin and re-measure{times}")
    return out


def summary(findings: list[Finding], anchored: int) -> str:
    ok = sum(1 for f in findings if f.ok is True)
    miss = sum(1 for f in findings if f.ok is False)
    unsure = sum(1 for f in findings if f.ok is None)
    parts = []
    if anchored:
        parts.append(f"{anchored} placed by OCR")
    if findings:
        parts.append(f"{ok} OK")
        parts.append(f"{miss} MISS")
        parts.append(f"{unsure} unverified")
    line = "grounding  " + ", ".join(parts)
    if miss:
        line += " — read the MISS lines above before embedding"
    elif unsure:
        line += " — read the .check twin for the unverified boxes"
    return line
