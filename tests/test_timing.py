"""The core invariant: durations and frame counts must land exactly.

"Looks about right" is not good enough here. Every one of these numbers is
derivable by hand from the spec, and a drift of even a few frames means the
cut/speed arithmetic or the concat is wrong.
"""

from __future__ import annotations

import pytest

from util import FPS, duration, frames, render

# (id, build-the-spec, expected seconds).  Source is 30s at 30fps = 900 frames.
CASES = [
    ("cut only",
     lambda m: {"cuts": [["0:00", "0:05"]]}, 25.0),
    ("speed up",
     lambda m: {"speed": [{"range": ["0:00", "0:10"], "factor": 2}]}, 25.0),
    ("slow down",
     lambda m: {"speed": [{"range": ["0:00", "0:10"], "factor": 0.5}]}, 40.0),
    ("overlapping cuts merge",
     lambda m: {"cuts": [["0:00", "0:10"], ["0:05", "0:15"]]}, 15.0),
    # A cut and a speed ramp over the same footage: the cut must win. auto-editor
    # on its own does the opposite and resurrects the cut range at the ramp speed.
    ("cut beats speed, same range",
     lambda m: {"cuts": [["0:10", "0:20"]],
                "speed": [{"range": ["0:10", "0:20"], "factor": 2}]}, 20.0),
    ("cut beats speed, partial overlap",
     lambda m: {"cuts": [["0:10", "0:15"]],
                "speed": [{"range": ["0:10", "0:25"], "factor": 2}]}, 20.0),
    ("slide at the very start",
     lambda m: {"slides": [{"at": 0, "text": "Intro"}]}, 33.0),
    ("slide at the very end",
     lambda m: {"slides": [{"at": "end", "text": "Fin"}]}, 33.0),
    ("slide past the end is clamped",
     lambda m: {"slides": [{"at": "9:99", "text": "Late"}]}, 33.0),
    ("two slides at one timestamp",
     lambda m: {"slides": [{"at": "0:10", "text": "A"}, {"at": "0:10", "text": "B"}]}, 36.0),
    ("slide inside a cut range",
     lambda m: {"cuts": [["0:05", "0:15"]], "slides": [{"at": "0:10", "text": "X"}]}, 23.0),
    ("image slide",
     lambda m: {"slides": [{"at": "0:10", "image": str(m.image), "seconds": 3}]}, 33.0),
    ("everything at once",
     lambda m: {"cuts": [["0:00", "0:05"]],
                "speed": [{"range": ["0:20", "0:25"], "factor": 2}],
                "slides": [{"at": "0:10", "text": "A", "seconds": 3},
                           {"at": "0:20", "image": str(m.image), "seconds": 3}]}, 28.5),
]


@pytest.mark.parametrize("build,expected", [pytest.param(b, e, id=i) for i, b, e in CASES])
def test_output_length_is_exact(tmp_path, media, build, expected):
    out = render(tmp_path, media.video, build(media))
    assert duration(out) == pytest.approx(expected, abs=0.02)
    assert frames(out) == round(expected * FPS)


def test_empty_spec_is_a_passthrough(tmp_path, media):
    out = render(tmp_path, media.video, {})
    assert duration(out) == pytest.approx(30.0, abs=0.05)


def test_notes_key_is_allowed_and_ignored(tmp_path, media):
    out = render(tmp_path, media.video, {"notes": "why I made these edits",
                                         "cuts": [["0:00", "0:05"]]})
    assert duration(out) == pytest.approx(25.0, abs=0.02)
