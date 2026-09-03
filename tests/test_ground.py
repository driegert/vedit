"""`ground.py`: the grounding check on hand-placed (x/y) boxes.

Two layers. The verdict logic runs on synthetic OCR rows (no tesseract): a hit inside
the box only counts when nothing on screen matches better, a MISS needs a full-score
hit, a partial fragment elsewhere is "could not verify", and coverage is measured
inside the outline. The end-to-end check runs tesseract on an image ffmpeg draws with
known text, through the CLI, and pins the output order (`grounding` lines and their
summary come *last*, after the `wrote` lines, so a `| tail` cannot hide a miss) and the
degraded path without tesseract.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vedit import ground, media, ocr, still
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


class FakeInfo:
    """Just enough MediaInfo for still.from_dict on an image."""
    is_image = True
    path = Path("frame.png")
    width, height = 1920, 1080


def spec_with_box(x, y, w, h, label, **extra):
    return still.from_dict({"highlights": [dict(x=x, y=y, w=w, h=h, label=label, **extra)]},
                           FakeInfo(), origin="test")


def verdict(rows, x, y, w, h, label, **extra):
    spec = spec_with_box(x, y, w, h, label, **extra)
    finding = ground.check_rows(spec, rows)[0]
    return finding, ground.report([finding])[0]


def test_a_better_match_elsewhere_beats_a_partial_one_inside_the_box():
    """Take 5, step 2: box on the 'RTools 4.4' row, 'RTools 4.5' two rows up."""
    rows = ocr.lines(row(262, "RTools", "4.5", x0=25) + row(295, "RTools", "4.4", x0=25)
                     + row(328, "RTools", "4.3", x0=25))
    f, line = verdict(rows, 20, 322, 360, 26, "Click: RTools 4.5")
    assert f.ok is False and f.found.y == 262
    assert "LIKELY MISS" in line and '"text": \'RTools 4.5\'' in line and "set x" not in line


def test_a_box_on_the_right_row_is_ok_and_counts_full_hits_only():
    rows = ocr.lines(row(262, "RTools", "4.5", x0=25) + row(295, "RTools", "4.4", x0=25))
    f, line = verdict(rows, 20, 255, 360, 30, "Click: RTools 4.5")
    assert f.ok is True and f.count == 1 and "inside the box" in line
    assert "appears" not in line


def test_the_same_text_twice_is_flagged():
    rows = ocr.lines(row(460, "Source", x0=653) + row(700, "Source", x0=653))
    f, line = verdict(rows, 640, 450, 200, 40, "Source")
    assert f.ok is True and f.count == 2 and "appears 2x" in line


def test_a_fragment_elsewhere_is_unverified_not_a_miss():
    """Take 5, step 20: the button caption was unreadable; 'restart' sat in a banner."""
    rows = ocr.lines(row(217, "require", "a", "restart", "to", "apply", x0=1500))
    f, line = verdict(rows, 1380, 256, 200, 24, "Click: Restart Extensions")
    assert f.ok is None and "only a fragment matched" in line and "could not verify" in line


def test_a_short_look_alike_never_moves_the_box():
    """Take 5, step 19: 'gear' used to match 'Sear' in the search box."""
    rows = ocr.lines(row(64, "Sear", x0=1886) + row(298, "Preview", "in", "Viewer", "Pane", x0=433))
    f, line = verdict(rows, 393, 165, 50, 55, "Click: gear icon")
    assert f.ok is None and f.found is None and "could not verify" in line


def test_generic_word_far_away_is_inconclusive():
    rows = ocr.lines(row(900, "Next", x0=1400))
    f, line = verdict(rows, 100, 100, 200, 40, "2. Click Next")
    assert f.ok is None and "proves nothing" in line


def test_coverage_is_measured_inside_the_outline():
    """Take 5, step 4: the red line ran through the filename and the check said covered."""
    rows = ocr.lines(row(516, "RStudio-2026.08.2-200.exe", x0=1180))
    # Box bottom lands exactly on the text's bottom edge (pad 22 + thickness 5 drawn inside).
    f, line = verdict(rows, 1175, 478, 310, 32, "Click: RStudio-2026.08.2-200.exe", pad=22, thickness=5)
    assert f.ok is not True, line
    f, line = verdict(rows, 1175, 500, 310, 40, "Click: RStudio-2026.08.2-200.exe", pad=22, thickness=5)
    assert f.ok is True, line


def test_text_anchored_boxes_are_not_second_guessed():
    r = still.Rect(0, 0, 10, 10)
    spec = still.StillSpec(highlights=[
        still.Highlight("box", r, r, r, 0, "red", (255, 0, 0), 3, label="RTools 4.5", text="RTools 4.5")])
    assert ground.check_rows(spec, []) == []
    assert ground.summary([], anchored=1) == "grounding  1 placed by OCR"


def test_summary_line():
    ok = ground.Finding(0, "a", "a", None, "a", 1.0, True)
    miss = ground.Finding(1, "b", "b", None, "b", 0.0, False)
    unsure = ground.Finding(2, "c", None, None, None, None, None)
    assert ground.summary([ok, miss, unsure], 2) == \
        "grounding  2 placed by OCR, 1 OK, 1 MISS, 1 unverified — read the MISS lines above before embedding"
    assert ground.summary([ok, unsure], 0).endswith("read the .check twin for the unverified boxes")


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
    assert abs(ok[0].found.x - 100) <= 6 and abs(ok[0].found.y - 200) <= 10

    # Same label, box 300 px to the right (over nothing): the text is elsewhere.
    miss = ground.check(spec_for(info, 400, 200, "Click Download RStudio"), info)
    assert miss[0].ok is False, ground.report(miss)
    line = ground.report(miss)[0]
    assert "LIKELY MISS" in line and "x 10" in line and "set x" not in line
    assert '"text": \'Download RStudio\'' in line

    # Box on the wrong control entirely: the label names the other button.
    wrong = ground.check(spec_for(info, 100, 200, "Click Restart Extensions"), info)
    assert wrong[0].ok is False and abs(wrong[0].found.x - 450) <= 6

    # A label naming nothing on screen is a shrug, not a miss.
    none = ground.check(spec_for(info, 100, 200, "Click the gear icon"), info)
    assert none[0].ok is None and "could not verify" in ground.report(none)[0]


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_cli_prints_grounding_last_with_a_summary(tmp_path):
    img = draw_text_image(tmp_path / "ui.png")
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"highlights": [{"x": 400, "y": 200, "w": 250, "h": 36,
                                                 "label": "Click Download RStudio"}]}))
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "still", str(img), str(spec),
                           "-o", str(tmp_path / "out.png")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    err = proc.stderr.splitlines()
    assert "grounding  highlight[0]" in err[-2] and "LIKELY MISS" in err[-2]
    assert err[-1] == "grounding  0 OK, 1 MISS, 0 unverified — read the MISS lines above before embedding"
    assert all(not line.startswith("grounding") for line in err[:-2])   # nothing before the wrote lines
    assert (tmp_path / "out.png").exists()  # a grounding miss never blocks the render


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_text_anchor_through_the_cli_and_its_sidecar(tmp_path):
    img = draw_text_image(tmp_path / "ui.png")
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"highlights": [{"text": "Restart Extensions", "label": "Click it"}]}))
    out = tmp_path / "out.png"
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "still", str(img), str(spec), "-o", str(out)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "text 'Restart Extensions' -> x 4" in proc.stderr and "(OCR)" in proc.stderr
    assert "you need not read it" in proc.stderr
    assert proc.stderr.splitlines()[-1] == "grounding  1 placed by OCR"
    sidecar = json.loads((tmp_path / "out.json").read_text())
    resolved = sidecar["highlights"][0]["resolved"]
    assert abs(resolved["x"] - 450) <= 6 and abs(resolved["y"] - 80) <= 10
    assert sidecar["highlights"][0]["text"] == "Restart Extensions"

    # Re-running from the sidecar resolves again, quietly, since nothing moved.
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "still", str(img), str(tmp_path / "out.json"),
                           "-o", str(out)], capture_output=True, text=True)
    assert proc.returncode == 0 and "note " not in proc.stderr, proc.stderr

    # A sidecar whose recorded place no longer matches the OCR reading says so.
    sidecar["highlights"][0]["resolved"] = {"x": 100, "y": 300, "w": 200, "h": 30}
    (tmp_path / "moved.json").write_text(json.dumps(sidecar))
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "still", str(img), str(tmp_path / "moved.json"),
                           "-o", str(out)], capture_output=True, text=True)
    assert proc.returncode == 0 and "note       highlights[0]: 'Restart Extensions' now resolves" in proc.stderr


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")
def test_text_anchor_errors_are_actionable(tmp_path):
    img = draw_text_image(tmp_path / "ui.png")
    info = media.probe(img, still=True)
    with pytest.raises(Exception) as exc:
        still.from_dict({"highlights": [{"text": "Download Positron"}]}, info, origin="test")
    msg = str(exc.value)
    assert "highlights[0].text" in msg and "not found" in msg and "Download RStudio" in msg
    with pytest.raises(Exception, match="not both"):
        still.from_dict({"highlights": [{"text": "Download RStudio", "x": 1, "y": 1, "w": 9, "h": 9}]},
                        info, origin="test")
    with pytest.raises(Exception, match='beside "text"'):
        still.from_dict({"highlights": [{"x": 1, "y": 1, "w": 9, "h": 9, "occurrence": 2}]},
                        info, origin="test")
    with pytest.raises(Exception, match="say where the box goes"):
        still.from_dict({"highlights": [{"label": "Click Next"}]}, info, origin="test")


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


def test_text_anchor_needs_tesseract(tmp_path, media):
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"at": 1, "highlights": [{"text": "Click here"}]}))
    env = dict(os.environ, VEDIT_NO_OCR="1")
    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "still", str(media.video), str(spec),
                           "-o", str(tmp_path / "out.png")], capture_output=True, text=True, env=env)
    assert proc.returncode == 1 and "needs tesseract" in proc.stderr
    assert not (tmp_path / "out.png").exists()
