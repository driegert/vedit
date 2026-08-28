"""A video with no audio stream.

auto-editor's default --edit audio method reads the audio stream and fails
outright without one, so this path needs --edit none and no audio codec.
"""

from __future__ import annotations

import pytest

from util import duration, has_audio, render


def test_slides_on_a_silent_video(tmp_path, media):
    out = render(tmp_path, media.silent, {"slides": [{"at": "0:10", "text": "Quiet"}]})
    assert duration(out) == pytest.approx(33.0, abs=0.02)
    assert not has_audio(out)


def test_cuts_and_speed_on_a_silent_video(tmp_path, media):
    out = render(tmp_path, media.silent, {
        "cuts": [["0:00", "0:05"]],
        "speed": [{"range": ["0:20", "0:25"], "factor": 2}],
    })
    assert duration(out) == pytest.approx(22.5, abs=0.02)


def test_audio_survives_when_the_source_has_it(tmp_path, media):
    out = render(tmp_path, media.video, {"slides": [{"at": "0:10", "text": "Loud"}]})
    assert has_audio(out)
