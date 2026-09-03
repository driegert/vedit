"""`ocr.py`: the matcher on synthetic word boxes (no tesseract), and `locate` / the text
dump end to end on an image ffmpeg draws with known text at known places.

The synthetic cases are the false verdicts from take 5 of the setup guide, reduced to
their mechanism: a short or digit-bearing token must match exactly and must be present
("RTools" is not a hit for "RTools 4.5"; "Sear" is not "gear"); a token OCR split in
two is rejoined ("RStudio-2026" + ".08.2-200.exe"); a hit is ranked by how much of the
query it matched, never by where it happens to sit.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vedit import VeditError, media, ocr
from vedit.geometry import Rect
from vedit.ocr import Word

HAS_TESSERACT = shutil.which("tesseract") is not None


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


# ---- tokens -----------------------------------------------------------------------------

@pytest.mark.parametrize("a, b, same", [
    ("rtools", "rtools", True),
    ("RTools:", "rtools", True),        # punctuation stripped by _norm
    ("docurt", "document", True),       # 4-char prefix on long tokens
    ("notebook", "ktetelbge", False),
    ("next", "next.", True),
    ("4.5", "4.5", True),
    ("4.5", "4.4", False),              # digits are exact
    ("x64", "x64.", True),
    ("gear", "sear", False),            # short tokens are exact: 0.75 similarity is not enough
    ("ok", "on", False),
    ("R-4.6.l", "r-4.6.1", True),       # OCR's l for 1 is folded in digit-bearing tokens
    ("RStudio-2026.@8.2-200.exe", "rstudio-2026.08.2-200.exe", True),   # and @ for 0
    ("windows", "window", True),
])
def test_token_match(a, b, same):
    assert ocr._token_match(ocr._norm(a), ocr._norm(b)) is same


def test_discriminating_tokens():
    assert ocr._discriminating("4.5") and ocr._discriminating("x64") and ocr._discriminating("next")
    assert not ocr._discriminating("download")


def test_queries_for_strips_instruction_words():
    assert ocr.queries_for("Click RTools 4.5")[0] == "RTools 4.5"
    assert ocr.queries_for("2. Click Next")[0] == "Next"
    assert ocr.queries_for('Press the "Download for x64" button') == ["Download for x64"]
    assert ocr.queries_for("OK") == []


# ---- lines and matching ------------------------------------------------------------------

def test_lines_groups_by_vertical_overlap_not_tesseract_blocks():
    words = row(100, "Quarto", "Document") + row(140, "Quarto", "Project") + row(103, "Quarto", x0=900)
    texts = sorted(" ".join(w.text for w in r) for r in ocr.lines(words))
    assert texts == ["Quarto", "Quarto Document", "Quarto Project"]


def test_dedupe_keeps_one_reading_per_box():
    a = Word("RTools", 25, 295, 66, 17, 90.0)
    b = Word("KLOOIS", 26, 296, 65, 16, 12.0)      # the same pixels read by the next tile
    c = Word("RTools", 25, 328, 66, 17, 90.0)
    assert ocr._dedupe([b, a, c]) == [a, c]


def test_a_missing_digit_token_voids_the_line():
    """The step 2 mechanism: 'RTools' alone must not count as half of 'RTools 4.5'."""
    rows = ocr.lines(row(262, "RTools", "4.5") + row(295, "RTools", "4.4") + row(328, "RTools"))
    hits = ocr.find("RTools 4.5", rows)
    assert [h.rect.y for h in hits] == [262]
    assert hits[0].score == 1.0


def test_partial_hits_still_count_for_long_words():
    rows = ocr.lines(row(10, "Don't", "create", "a", "Start", "Menu") + row(40, "Install", "now"))
    hits = ocr.find("Don't create a Start Menu folder", rows)
    assert hits and hits[0].rect.y == 10 and hits[0].score == pytest.approx(5 / 6)
    assert ocr.find("Don't create a Start Menu folder", rows, strict=True)       # 5 of 6 will do
    assert ocr.find("Don't create a Start Menu folder now please", rows, strict=True) == []


def test_exact_outranks_fuzzy_and_every_occurrence_on_a_line_counts():
    """t = 72.6: 'Rtools45 may be installed from the Rtools45 installer or 64-bit ARM
    Rtools45 installer' — one fuzzy reading and two exact ones on the same line."""
    rows = ocr.lines(row(678, "Rtools45", "may", "be", "installed", "from", "the", "Rtools45", "installer",
                         "or", "64-bit", "ARM", "Rtools45", "installer."))
    hits = ocr.find("Rtools45 installer", rows, strict=True)
    assert [round(h.score, 2) for h in hits] == [1.0, 1.0, 0.95]
    assert hits[0].rect.x < hits[1].rect.x < hits[2].rect.x or hits[2].rect.x == 100
    with pytest.raises(VeditError, match="appears 2 times"):
        ocr.locate("Rtools45 installer", rows)
    assert ocr.locate("Rtools45 installer", rows, occurrence=1).text == "Rtools45 installer"


def test_a_garbled_word_stands_in_for_a_long_token_only():
    rows = ocr.lines(row(333, "Quarto", ("Docurt", 0.0)) + row(366, "Quarto", ("4.#", 0.0)))
    assert [h.rect.y for h in ocr.find("Quarto Document", rows)] == [333, 366]   # both half-credit
    assert ocr.find("Quarto 4.5", rows) == []                                   # never for a digit


def test_junk_between_two_words_is_skipped():
    rows = ocr.lines(row(652, "For", "anyone", "who", "uses", "this", "OS", "ee", "computer", "(all", "users)"))
    hits = ocr.find("For anyone who uses this computer (all users)", rows, strict=True)
    assert hits and hits[0].score == 1.0
    assert ocr.find("For anyone who uses this computer", ocr.lines(row(1, "For", "anyone", "a", "b", "c", "computer"))) == []


def test_a_dropped_short_word_still_locates():
    rows = ocr.lines(row(543, "Install", "just", "for", "(drieg)") + row(668, "Install", "for", "all"))
    hit = ocr.locate("Install just for you", rows)
    assert hit.rect.y == 543 and hit.score == 0.75


def test_a_digit_look_alike_is_an_exact_match():
    """t = 121: tesseract read the 0 as @; that reading must score as the filename, in full."""
    rows = ocr.lines(row(516, "RStudio-2026.@8.2-200.exe") + row(583, "RStudio-2026.08.2-200.zip"))
    hits = ocr.find("RStudio-2026.08.2-200.exe", rows, strict=True)
    assert [h.rect.y for h in hits] == [516] and hits[0].score == 1.0


def test_a_split_token_is_rejoined():
    rows = ocr.lines(row(516, "RStudio-2026", ".08.2-200.exe") + row(583, "RStudio-2026", ".08.2-200.zip"))
    hits = ocr.find("RStudio-2026.08.2-200.exe", rows, strict=True)
    assert [h.rect.y for h in hits] == [516]
    assert hits[0].rect.w == 9 * len("RStudio-2026") + 8 + 9 * len(".08.2-200.exe")


def test_short_word_alone_never_fuzzy_matches():
    rows = ocr.lines(row(64, "Sear", x0=1886) + row(300, "Preview", "in", "Viewer", "Pane"))
    assert ocr.find("gear icon", rows) == []


def test_hits_are_ranked_by_score_then_position():
    rows = ocr.lines(row(400, "Install", "just", "for") + row(200, "Install", "just", "for", "you"))
    hits = ocr.find("Install just for you", rows)
    assert [h.rect.y for h in hits] == [200, 400]


# ---- locate -------------------------------------------------------------------------------

def test_locate_single_hit():
    rows = ocr.lines(row(262, "RTools", "4.5") + row(295, "RTools", "4.4"))
    hit = ocr.locate("RTools 4.5", rows)
    assert hit.rect.y == 262 and hit.line == "RTools 4.5"


def test_locate_ambiguous_lists_each_place_and_takes_occurrence_or_near():
    rows = ocr.lines(row(460, "Source", x0=653) + row(460, "Source", x0=900) + row(700, "Source", x0=653))
    with pytest.raises(VeditError) as exc:
        ocr.locate("Source", rows, where="highlights[0].text")
    msg = str(exc.value)
    assert "appears 3 times" in msg and "#1" in msg and "#3" in msg and "occurrence" in msg and "near" in msg
    assert ocr.locate("Source", rows, occurrence=2).rect.x == 900
    assert ocr.locate("Source", rows, occurrence=3).rect.y == 700
    assert ocr.locate("Source", rows, near=(660, 690)).rect.y == 700
    with pytest.raises(VeditError, match="occurrence 4"):
        ocr.locate("Source", rows, occurrence=4)


def test_locate_not_found_lists_the_closest_lines_and_the_fallback():
    rows = ocr.lines(row(100, "Download", "Quarto", "CLI", "1.10.18") + row(200, "Something", "else"))
    with pytest.raises(VeditError) as exc:
        ocr.locate("Download Quarto CLI for Windows", rows, where="highlights[0].text")
    msg = str(exc.value)
    assert "was not found on screen" in msg
    assert "Download Quarto CLI 1.10.18" in msg          # closest line, with its box
    assert "x 100 y 100" in msg
    assert '"x", "y", "w", "h"' in msg                   # the measured fallback is named


# ---- the dump -----------------------------------------------------------------------------

def test_dump_is_top_to_bottom_with_boxes_and_grep():
    rows = ocr.lines(row(295, "RTools", "4.4") + row(262, "RTools", "4.5") + row(20, "Choose", "your", "version"))
    out = ocr.dump(rows)
    assert [line.split()[3] for line in out] == ["20", "262", "295"]
    assert out[1].startswith("x  100 y  262 w") and out[1].endswith("  RTools 4.5")
    assert ocr.dump(rows, "4\\.5") == [out[1]]
    assert ocr.dump(rows, "nothing") == []


# ---- end to end on a drawn image -----------------------------------------------------------

def draw(path: Path, *texts: tuple[str, int, int]) -> Path:
    vf = ",".join(f"drawtext=text='{t}':x={x}:y={y}:fontsize=30:fontcolor=black" for t, x, y in texts)
    subprocess.run([media.ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=800x400",
                    "-frames:v", "1", "-update", "1", "-vf", vf, str(path)], check=True)
    return path


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_locate_on_a_drawn_image(tmp_path):
    img = draw(tmp_path / "ui.png", ("Download RStudio", 100, 200), ("Restart Extensions", 450, 80))
    info = media.probe(img, still=True)
    rows = ocr.rows_for(info, None)
    hit = ocr.locate("Download RStudio", rows)
    assert abs(hit.rect.x - 100) <= 6 and abs(hit.rect.y - 200) <= 10
    with pytest.raises(VeditError, match="not found"):
        ocr.locate("Download Positron", rows)


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_ocr_command_lists_text_with_boxes(tmp_path):
    img = draw(tmp_path / "ui.png", ("Download RStudio", 100, 200), ("Restart Extensions", 450, 80))
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "ocr", str(img)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert any("Download RStudio" in line and line.startswith("x ") for line in lines)
    assert int(lines[0].split()[3]) < int(lines[-1].split()[3])   # top to bottom
    assert "boxes are full-frame pixels" in proc.stderr and "OCR often misses" in proc.stderr

    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "ocr", str(img), "--grep", "restart"],
                          capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.count("\n") == 1 and "Restart" in proc.stdout

    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "ocr", str(img), "--at", "3"],
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "--at does not apply" in proc.stderr


def test_ocr_command_on_a_video_needs_at(media):
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "ocr", str(media.video)],
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "needs --at" in proc.stderr
