"""EBU R128 loudness normalisation.

The source recordings this was built for came in around -38 LUFS, quiet enough
that the two-pass measure-then-apply is the whole point.
"""

from __future__ import annotations

import pytest

from util import duration, loudness, render, run_vedit


def test_a_quiet_source_is_brought_up_to_target(tmp_path, media):
    before = loudness(media.quiet)
    assert before < -25, f"the fixture should be quiet, measured {before}"

    out = render(tmp_path, media.quiet, {"audio": {"normalize": "ebu", "target": -16}})
    assert loudness(out) == pytest.approx(-16.0, abs=1.5)


def test_a_custom_target_is_honoured(tmp_path, media):
    out = render(tmp_path, media.quiet, {"audio": {"normalize": "ebu", "target": -23}})
    assert loudness(out) == pytest.approx(-23.0, abs=1.5)


def test_normalising_does_not_change_the_duration(tmp_path, media):
    out = render(tmp_path, media.quiet, {"audio": {"normalize": "ebu"}})
    assert duration(out) == pytest.approx(duration(media.quiet), abs=0.05)


def test_normalising_combines_with_cuts(tmp_path, media):
    out = render(tmp_path, media.quiet, {
        "cuts": [["0:00", "0:05"]],
        "audio": {"normalize": "ebu", "target": -16},
    })
    assert duration(out) == pytest.approx(25.0, abs=0.05)
    assert loudness(out) == pytest.approx(-16.0, abs=1.5)


def test_normalising_a_silent_source_is_rejected(tmp_path, media):
    out = tmp_path / "out.mp4"
    proc = run_vedit(tmp_path, media.silent, {"audio": {"normalize": "ebu"}}, out)
    assert proc.returncode != 0
    assert "no audio" in proc.stderr


def test_click_transients_are_limited_rather_than_capping_the_gain(tmp_path, media):
    """A screen recording: voice far below target, clicks near full scale.

    Without the limiter the gain is capped by the clicks and the voice lands ~8 dB
    short; with it the voice reaches the target and the clicks stay under -1.5 dBTP.
    """
    from util import true_peak

    from vedit.render import MAX_LIMITING, TRUE_PEAK_CEILING

    before, peak_before = loudness(media.clicky), true_peak(media.clicky)
    assert before < -24, f"fixture should be quiet: {before} LUFS"
    # The precondition that makes this test mean anything: a plain gain would breach
    # the ceiling before the tone reached -16 LUFS -- and by less than the limiter is
    # allowed to absorb, so the limiter branch (not the cap) is what runs.
    excess = (-16 - before) - (TRUE_PEAK_CEILING - peak_before)
    assert 0 < excess <= MAX_LIMITING - 1, f"fixture: {before} LUFS, {peak_before} dBTP"

    out = tmp_path / "out.mp4"
    proc = run_vedit(tmp_path, media.clicky, {"audio": {"normalize": "ebu", "target": -16}}, out)
    assert proc.returncode == 0, proc.stderr
    assert loudness(out) == pytest.approx(-16.0, abs=1.5)
    assert true_peak(out) <= TRUE_PEAK_CEILING + 0.2      # AAC overshoot only


def test_peak_normalisation_targets_true_peak(tmp_path, media):
    """peak mode works in dBTP and defaults to the same ceiling ebu respects."""
    import json
    import re
    import subprocess

    out = render(tmp_path, media.quiet, {"audio": {"normalize": "peak"}})
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(out),
         "-af", "loudnorm=I=-24:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    blob = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.DOTALL)
    assert blob
    assert float(json.loads(blob.group(0))["input_tp"]) == pytest.approx(-1.5, abs=0.6)
