"""Guarding the source file and leaving no debris behind."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys

import pytest

from util import duration, render, run_vedit


def _md5(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


def test_writing_over_the_source_is_refused(tmp_path, media):
    """An agent that reuses the input path must not destroy the recording."""
    source = tmp_path / "lecture.mp4"
    shutil.copy(media.video, source)
    before = _md5(source)

    proc = run_vedit(tmp_path, source, {"cuts": [["0:00", "0:05"]]}, source)

    assert proc.returncode != 0
    assert "same file as the source" in proc.stderr
    assert _md5(source) == before, "the source file was modified"


def test_a_failed_render_leaves_no_debris(tmp_path, media):
    out = tmp_path / "out.mp4"
    proc = run_vedit(tmp_path, media.video, {"cuts": [["0:40", "0:50"]]}, out)

    assert proc.returncode != 0
    assert not out.exists()
    assert not list(tmp_path.glob(".vedit-*")), "a staging file was left behind"


def test_dry_run_writes_nothing(tmp_path, media):
    out = tmp_path / "out.mp4"
    proc = run_vedit(tmp_path, media.video, {"cuts": [["0:00", "0:05"]]}, out, "--dry-run")

    assert proc.returncode == 0
    assert not out.exists()
    assert "dry run" in proc.stderr


@pytest.mark.parametrize("spec,expected", [
    ({"cuts": [["0:00", "0:05"]]}, 25.0),
    ({"cuts": [["0:10", "0:15"]], "speed": [{"range": ["0:10", "0:25"], "factor": 2}]}, 20.0),
    ({"cuts": [["0:00", "0:05"]], "slides": [{"at": "0:10", "text": "A", "seconds": 3}]}, 28.0),
])
def test_dry_run_estimate_matches_the_real_render(tmp_path, media, spec, expected):
    """The estimate is what an agent checks before committing to a long render."""
    out = tmp_path / "out.mp4"
    proc = run_vedit(tmp_path, media.video, spec, out, "--dry-run")
    assert proc.returncode == 0, proc.stderr

    match = re.search(r"=\s*([\d.]+)s\s*$", proc.stderr, re.MULTILINE)
    assert match, f"no estimate line found in:\n{proc.stderr}"
    estimate = float(match.group(1))

    assert estimate == pytest.approx(expected, abs=0.02)
    assert duration(render(tmp_path, media.video, spec)) == pytest.approx(estimate, abs=0.02)


def test_output_goes_to_a_new_directory(tmp_path, media):
    out = render(tmp_path, media.video, {"cuts": [["0:00", "0:05"]]},
                 name="nested/deeper/out.mp4")
    assert out.exists()


def _cli(*args):
    return subprocess.run([sys.executable, "-m", "vedit.cli", *args],
                          capture_output=True, text=True)


def test_probe_reports_the_source(media):
    proc = _cli("probe", str(media.video))
    assert proc.returncode == 0
    assert "30.00s" in proc.stdout and "640x360" in proc.stdout


def test_probe_json_is_machine_readable(media):
    import json
    proc = _cli("probe", str(media.video), "--json")
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["frames"] == 900 and data["has_audio"] is True


def test_example_is_printable_json(media):
    import json
    proc = _cli("example")
    assert proc.returncode == 0
    json.loads(proc.stdout)
