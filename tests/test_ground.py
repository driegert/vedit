"""`ground.py`: the OCR grounding check behind `vedit still`.

Two layers. The matcher is exercised on synthetic word boxes (no tesseract needed): label
→ query derivation, fuzzy tokens, line grouping, partial matches through a cursor-garbled
word, the generic-word guard. The end-to-end check runs tesseract on an image ffmpeg draws
with known text at known coordinates, and is skipped where tesseract is not installed —
the feature degrades to a one-line note there, which `test_cli_skips_without_tesseract`
pins down by forcing `VEDIT_NO_OCR`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vedit import ground, media, still
from vedit.ground import Word

HAS_TESSERACT = shutil.which("tesseract") is not None


# ---- label -> query ---------------------------------------------------------------------

@pytest.mark.parametrize("label, first", [
    ("Click RTools 4.5", "RTools 4.5"),
    ("2. Click Next", "Next"),
    ("Pick: Quarto Document", "Quarto Document"),
    ("1. Check: Don't create a Start Menu folder", "Don't create a Start Menu folder"),
    ('Press the "Download for x64" button', "Download for x64"),
    ("Windows 11 - pick the .exe", "Windows 11 - pick the .exe"),
    ("Select Install just for you", "Install just for you"),
])
def test_queries_for_strips_instruction_words(label, first):
    assert ground.queries_for(label)[0] == first


def test_queries_for_splits_on_dashes_and_colons_as_fallbacks():
    qs = ground.queries_for("Windows 11 - pick the .exe")
    assert qs == ["Windows 11 - pick the .exe", "Windows 11", ".exe"] or qs[:2] == [
        "Windows 11 - pick the .exe", "Windows 11"]


def test_queries_for_short_labels_yield_nothing():
    assert ground.queries_for("OK") == []
    assert ground.queries_for("1.") == []


# ---- tokens -----------------------------------------------------------------------------

@pytest.mark.parametrize("a, b, same", [
    ("rtools", "rtools", True),
    ("RTools:", "rtools", True),        # via _norm in Word.token; here raw compare is False
    ("docurt", "document", True),       # 4-char prefix
    ("notebook", "ktetelbge", False),
    ("next", "next.", True),
    ("4.5", "4.5", True),
    ("ok", "on", False),                # short tokens must be exact
])
def test_token_match(a, b, same):
    assert ground._token_match(ground._norm(a), ground._norm(b)) is same


# ---- line grouping and matching on synthetic boxes ---------------------------------------

def row(y, *texts, x0=100, gap=8, h=16, conf=95.0):
    words, x = [], x0
    for t in texts:
        c = conf
        if isinstance(t, tuple):
            t, c = t
        w = 9 * len(t)
        words.append(Word(t, x, y, w, h, c))
        x += w + gap
    return words


def test_lines_groups_by_vertical_overlap_not_tesseract_blocks():
    words = row(100, "Quarto", "Document") + row(140, "Quarto", "Project") + row(103, "Quarto", x0=900)
    rows = ground.lines(words)
    texts = sorted(" ".join(w.text for w in r) for r in rows)
    # The far-right "Quarto" at y=103 is on the first line's band but 700 px away: its own line.
    assert texts == ["Quarto", "Quarto Document", "Quarto Project"]


def test_find_matches_through_a_garbled_word():
    rows = ground.lines(row(333, "Quarto", ("Docurt", 0.0), "Q", "to.") + row(366, "Quarto", "Project"))
    hits = ground.find("Quarto Document", rows)
    assert hits, "no match"
    score, box, text = hits[0]
    assert text.startswith("Quarto")
    assert box.x == 100 and box.y == 333
    # Both rows match "Quarto"; the garbled row still ranks first or equal.
    assert score >= 0.5


def test_find_needs_half_the_tokens():
    rows = ground.lines(row(10, "Install", "R", "now"))
    assert ground.find("Restart Extensions Please", rows) == []
    assert ground.find("Install now", rows)


def test_generic_guard():
    assert ground._generic("Next")
    assert ground._generic("OK")
    assert not ground._generic("Restart")
    assert not ground._generic("Quarto Document")


# ---- end to end: ffmpeg draws known text, tesseract must find it --------------------------

def draw_text_image(path: Path) -> Path:
    """White 800x400 image with two labelled 'buttons' at known places."""
    vf = ("drawtext=text='Download RStudio':x=100:y=200:fontsize=30:fontcolor=black,"
          "drawtext=text='Restart Extensions':x=450:y=80:fontsize=30:fontcolor=black")
    subprocess.run([media.ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=800x400",
                    "-frames:v", "1", "-update", "1", "-vf", vf, str(path)], check=True)
    return path


def spec_for(info, x, y, label, w=250, h=36):
    return still.from_dict({"highlights": [{"x": x, "y": y, "w": w, "h": h, "label": label}]},
                           info, origin="test")


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_check_covered_and_missed_on_a_drawn_image(tmp_path):
    img = draw_text_image(tmp_path / "ui.png")
    info = media.probe(img, still=True)

    ok = ground.check(spec_for(info, 100, 200, "Click Download RStudio"), info)
    assert len(ok) == 1 and ok[0].ok is True, ground.report(ok)
    assert abs(ok[0].found.x - 100) <= 6 and abs(ok[0].found.y - 200) <= 8

    # Same label, box 300 px to the right (over nothing): the text is elsewhere.
    miss = ground.check(spec_for(info, 400, 200, "Click Download RStudio"), info)
    assert miss[0].ok is False, ground.report(miss)
    line = ground.report(miss)[0]
    assert "LIKELY MISS" in line and "set x 1" in line  # x≈100, printed for the fix

    # Box on the wrong control entirely: the label names the other button.
    wrong = ground.check(spec_for(info, 100, 200, "Click Restart Extensions"), info)
    assert wrong[0].ok is False and abs(wrong[0].found.x - 450) <= 6

    # A label naming nothing on screen is a shrug, not a miss.
    none = ground.check(spec_for(info, 100, 200, "Click the gear icon"), info)
    assert none[0].ok is None and "verify by eye" in ground.report(none)[0]


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_cli_prints_grounding_lines(tmp_path):
    img = draw_text_image(tmp_path / "ui.png")
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"highlights": [{"x": 400, "y": 200, "w": 250, "h": 36,
                                                 "label": "Click Download RStudio"}]}))
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "still", str(img), str(spec),
                           "-o", str(tmp_path / "out.png")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "grounding  highlight[0]" in proc.stderr and "LIKELY MISS" in proc.stderr
    assert (tmp_path / "out.png").exists()  # a grounding miss never blocks the render


def test_cli_skips_without_tesseract(tmp_path, media):
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"at": 1, "highlights": [{"x": 125, "y": 90, "w": 80, "h": 60,
                                                          "label": "Click here"}]}))
    env = dict(os.environ, VEDIT_NO_OCR="1")
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "still", str(media.video), str(spec),
                           "-o", str(tmp_path / "out.png")], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "skipping the OCR check" in proc.stderr
    assert "grounding  highlight" not in proc.stderr
