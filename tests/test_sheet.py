"""`vedit sheet`: several moments on one labelled contact sheet.

The invariant is the exact output size (cells x columns, rows from the count), and
that each cell carries its time stamp: the label box darkens the top-left corner of a
cell that is otherwise the bright testsrc2 pattern.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from util import region_mean, resolution


def run_sheet(video: Path, out: Path, *times: str, extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "vedit.cli", "sheet", str(video), *times,
                           "-o", str(out), *extra], capture_output=True, text=True)


def test_sheet_size_is_cells_by_grid(tmp_path, media):
    out = tmp_path / "sheet.png"
    proc = run_sheet(media.video, out, "1", "2.5", "0:04", "10")
    assert proc.returncode == 0, proc.stderr
    assert resolution(out) == (3 * 640, 2 * 360)          # 640x360 source: cells are 1:1
    assert proc.stdout.strip() == str(out)
    assert "4 frames" in proc.stderr and "stamped with its time" in proc.stderr


def test_cells_carry_a_time_label(tmp_path, media):
    out = tmp_path / "sheet.png"
    assert run_sheet(media.video, out, "1", "2", extra=("--width", "320")).returncode == 0
    assert resolution(out) == (3 * 320, 180)
    # The label sits at (8, 8) of each cell as yellow on a black box: that corner is far
    # darker than the same corner of the empty third cell (tile fills it with black too,
    # so compare against a bright patch of the second cell instead).
    label = region_mean(out, 0, 10, 10, 60, 24)
    body = region_mean(out, 0, 320 + 100, 100, 320 + 200, 150)
    assert label < body


def test_limits_and_errors(tmp_path, media):
    out = tmp_path / "sheet.jpg"
    proc = run_sheet(media.video, out, *[str(i) for i in range(13)])
    assert proc.returncode == 1 and "at most 12" in proc.stderr and not out.exists()
    proc = run_sheet(media.video, out, "5", "45")
    assert proc.returncode == 1 and "past the end" in proc.stderr
    proc = run_sheet(media.video, out, "5", extra=("--cols", "9"))
    assert proc.returncode == 1 and "--cols" in proc.stderr
    proc = run_sheet(media.image, out, "1")
    assert proc.returncode == 1 and "needs a video" in proc.stderr
    proc = run_sheet(media.video, tmp_path / "sheet.gif", "1")
    assert proc.returncode == 1 and ".jpg or .png" in proc.stderr
