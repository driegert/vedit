"""Real screencast frames against the text-anchor resolver and the grounding check.

The frames are not committed (they show a personal desktop) — see `fixtures/README.md`;
everything here skips when `fixtures/frames/` is empty. `ground_truth.json` carries the
cases: where each anchor text must resolve, which labels are ambiguous or unreadable,
and hand-placed boxes with the verdict the check must give. Several are the exact false
verdicts that sent take 5 of the setup guide wrong, so they must never come back.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from vedit import VeditError, ground, media, ocr, still

HERE = Path(__file__).parent / "fixtures"
FRAMES = HERE / "frames"
TRUTH = json.loads((HERE / "ground_truth.json").read_text())

pytestmark = pytest.mark.skipif(
    shutil.which("tesseract") is None or not any(FRAMES.glob("t*.png")),
    reason="needs tesseract and the extracted fixture frames (tests/fixtures/extract.sh)",
)


def frame(t: str) -> Path:
    path = FRAMES / f"t{t}.png"
    if not path.exists():
        pytest.skip(f"fixture frame {path.name} not extracted")
    return path


def rows(t: str):
    return ocr.rows_for(media.probe(frame(t), still=True), None)


def close(found, expect: dict, tol: int) -> bool:
    return (abs(found.x - expect["x"]) <= tol and abs(found.y - expect["y"]) <= tol
            and abs(found.right - (expect["x"] + expect["w"])) <= tol
            and abs(found.bottom - (expect["y"] + expect["h"])) <= tol)


@pytest.mark.parametrize("case", [c for c in TRUTH["anchors"] if "expect" in c],
                         ids=lambda c: f"{c['t']}:{c['text']}")
def test_anchor_resolves_where_the_text_is(case):
    r = rows(case["t"])
    if case.get("ambiguous"):
        with pytest.raises(VeditError, match="appears .* times"):
            ocr.locate(case["text"], r)
        hit = ocr.locate(case["text"], r, near=tuple(case["near"]))
    else:
        hit = ocr.locate(case["text"], r)
    assert close(hit.rect, case["expect"], case.get("tol", 16)), \
        f"{case['text']!r} resolved to {hit.rect}, expected {case['expect']} (line: {hit.line!r})"


@pytest.mark.parametrize("case", [c for c in TRUTH["anchors"] if c.get("unreadable")],
                         ids=lambda c: f"{c['t']}:{c['text']}")
def test_known_unreadable_text_is_refused_not_guessed(case):
    """Light-on-dark captions and the dark terminal: OCR must say so, listing what it did
    read, rather than settle on a look-alike. If this starts failing, OCR got better —
    move the case up to `anchors` with its coordinates."""
    with pytest.raises(VeditError, match="was not found on screen") as exc:
        ocr.locate(case["text"], rows(case["t"]))
    assert "measured on the gridded frame" in str(exc.value)


@pytest.mark.parametrize("case", TRUTH["grounding"], ids=lambda c: f"{c['t']}:{c['verdict']}:{c['label']}")
def test_hand_placed_box_gets_the_right_verdict(case):
    info = media.probe(frame(case["t"]), still=True)
    spec = still.from_dict({"highlights": [dict(case["box"], label=case["label"])]}, info, origin="test")
    finding = ground.check_rows(spec, ocr.rows_for(info, None))[0]
    line = ground.report([finding])[0]
    if case["verdict"] == "ok":
        assert finding.ok is True, line
    elif case["verdict"] == "miss":
        assert finding.ok is False and "LIKELY MISS" in line, line
    elif case["verdict"] == "not_ok":
        assert finding.ok is not True, line
    else:
        assert finding.ok is None, line
        assert "could not verify" in line or "proves nothing" in line


def test_the_anchor_form_beats_a_wrong_hand_box_on_the_same_frame():
    """The take 5 step 2 story in one test: the hand box is a MISS, the anchor is right."""
    info = media.probe(frame("64"), still=True)
    hand = still.from_dict({"highlights": [{"x": 20, "y": 322, "w": 360, "h": 26,
                                            "label": "Click: RTools 4.5"}]}, info, origin="test")
    assert ground.check(hand, info)[0].ok is False
    anchored = still.from_dict({"highlights": [{"text": "RTools 4.5"}]}, info, origin="test")
    h = anchored.highlights[0]
    assert h.text == "RTools 4.5" and h.label == "RTools 4.5"
    assert 250 <= h.target.y <= 275 and h.resolved == h.target
    assert ground.check(anchored, info) == []      # nothing left to second-guess
