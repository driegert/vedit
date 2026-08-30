"""Malformed specs must fail loudly and cleanly.

An agent writing a bad spec should get a message naming the offending key. What
it must never get is a traceback, or worse, a video that silently ignored the
entry and looks plausible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from util import run_vedit

BAD_SPECS = [
    ("unknown top-level key",     lambda m: {"cutz": [["0:00", "0:05"]]}),
    ("unknown slide key",         lambda m: {"slides": [{"at": 0, "text": "A", "durration": 3}]}),
    ("unknown speed key",         lambda m: {"speed": [{"range": ["0:00", "0:05"],
                                                        "factor": 2, "easing": "linear"}]}),
    ("cuts is not a list",        lambda m: {"cuts": {"from": 0}}),
    ("cut is not a pair",         lambda m: {"cuts": [["0:00"]]}),
    ("backwards range",           lambda m: {"cuts": [["0:20", "0:10"]]}),
    ("zero-length range",         lambda m: {"cuts": [["0:10", "0:10"]]}),
    ("range starts past the end", lambda m: {"cuts": [["0:40", "0:50"]]}),
    ("negative time",             lambda m: {"cuts": [[-5, "0:10"]]}),
    ("unparseable time",          lambda m: {"cuts": [["half past two", "0:10"]]}),
    ("timecode-ish nonsense",     lambda m: {"cuts": [["0:0:0:0", "0:10"]]}),
    ("speed missing factor",      lambda m: {"speed": [{"range": ["0:00", "0:05"]}]}),
    ("zero speed factor",         lambda m: {"speed": [{"range": ["0:00", "0:05"], "factor": 0}]}),
    ("negative speed factor",     lambda m: {"speed": [{"range": ["0:00", "0:05"], "factor": -2}]}),
    ("overlapping speed ranges",  lambda m: {"speed": [
        {"range": ["0:05", "0:15"], "factor": 2}, {"range": ["0:10", "0:20"], "factor": 3}]}),
    ("slide missing at",          lambda m: {"slides": [{"text": "A"}]}),
    ("slide with neither",        lambda m: {"slides": [{"at": 0}]}),
    ("slide with both",           lambda m: {"slides": [{"at": 0, "text": "A",
                                                         "image": str(m.image)}]}),
    ("missing image file",        lambda m: {"slides": [{"at": 0, "image": "/nope/x.png"}]}),
    ("non-numeric seconds",       lambda m: {"slides": [{"at": 0, "text": "A",
                                                         "seconds": "three"}]}),
    ("infinite seconds",          lambda m: {"slides": [{"at": 0, "text": "A",
                                                         "seconds": "inf"}]}),
    ("boolean where time expected", lambda m: {"slides": [{"at": True, "text": "A"}]}),
    ("colour with filter syntax", lambda m: {"slides": [{"at": 0, "text": "A",
                                                         "color": "white:box=1:boxcolor=red"}]}),
    ("chapter_titles without chapters", lambda m: {"chapter_titles": True}),
    ("chapter_titles bad seconds",  lambda m: {"chapters": [{"at": 0, "title": "A"}],
                                               "chapter_titles": {"seconds": 0}}),
    ("chapter_titles bad position", lambda m: {"chapters": [{"at": 0, "title": "A"}],
                                               "chapter_titles": {"position": "left"}}),
    ("unknown chapter_titles key",  lambda m: {"chapters": [{"at": 0, "title": "A"}],
                                               "chapter_titles": {"duration": 4}}),
    ("chapter_titles zero",         lambda m: {"chapters": [{"at": 0, "title": "A"}],
                                               "chapter_titles": 0}),
    ("chapter_titles list",         lambda m: {"chapters": [{"at": 0, "title": "A"}],
                                               "chapter_titles": []}),
    ("spec is not an object",     lambda m: [{"cuts": []}]),
    ("cuts remove everything",    lambda m: {"cuts": [["start", "end"]]}),
    # --- floating text, chapters and audio ---
    ("caption text is null",      lambda m: {"text": [{"range": ["0:00", "0:05"],
                                                       "text": None}]}),
    ("caption text is empty",     lambda m: {"text": [{"range": ["0:00", "0:05"],
                                                       "text": "  "}]}),
    ("empty speed label",         lambda m: {"speed": [{"range": ["0:00", "0:05"],
                                                        "factor": 2, "label": ""}]}),
    ("bad caption position",      lambda m: {"text": [{"range": ["0:00", "0:05"],
                                                       "text": "x", "position": "middleish"}]}),
    ("caption hidden by a cut",   lambda m: {"cuts": [["0:05", "0:20"]],
                                             "text": [{"range": ["0:08", "0:12"],
                                                       "text": "gone"}]}),
    ("chapter with no title",     lambda m: {"chapters": [{"at": 0, "title": "  "}]}),
    ("chapter past the end",      lambda m: {"chapters": [{"at": "9:99", "title": "Late"}]}),
    ("chapters collapsed by a cut", lambda m: {"cuts": [["0:10", "0:20"]],
                                               "chapters": [{"at": 0, "title": "Zero"},
                                                            {"at": "0:12", "title": "A"},
                                                            {"at": "0:15", "title": "B"}]}),
    ("chapter cut off the end",   lambda m: {"cuts": [["0:20", "end"]],
                                             "chapters": [{"at": 0, "title": "A"},
                                                          {"at": "0:25", "title": "B"}]}),
    ("toc card with no chapters", lambda m: {"toc_card": True}),
    ("audio target, no normalize", lambda m: {"audio": {"target": -16}}),
    ("ebu target above -5",       lambda m: {"audio": {"normalize": "ebu", "target": -2}}),
    ("unknown normalizer",        lambda m: {"audio": {"normalize": "rms"}}),
    ("unknown audio key",         lambda m: {"audio": {"normalize": "ebu", "loud": True}}),
]


@pytest.mark.parametrize("build", [pytest.param(b, id=i) for i, b in BAD_SPECS])
def test_bad_spec_is_rejected_cleanly(tmp_path, media, build):
    out = tmp_path / "out.mp4"
    proc = run_vedit(tmp_path, media.video, build(media), out)

    assert proc.returncode != 0, f"expected a failure, got success:\n{proc.stderr}"
    assert "Traceback" not in proc.stderr, f"leaked a traceback:\n{proc.stderr}"
    assert "error:" in proc.stderr, f"no readable error message:\n{proc.stderr}"
    assert not out.exists(), "a rejected spec still produced an output file"


def test_a_good_spec_still_succeeds(tmp_path, media):
    """Guards the suite above against becoming vacuously true."""
    out = tmp_path / "out.mp4"
    proc = run_vedit(tmp_path, media.video, {"cuts": [["0:00", "0:05"]]}, out)
    assert proc.returncode == 0, proc.stderr
    assert out.exists()


def test_the_printed_example_validates_for_a_real_lecture():
    """The example is what an agent copies, so it must survive validation.

    It is checked against a synthetic 20-minute MediaInfo rather than the 30s
    fixture, because the example quite reasonably refers to times like 14:05.
    """
    import json
    import subprocess
    import sys
    from fractions import Fraction

    from vedit import media, spec as spec_mod

    proc = subprocess.run([sys.executable, "-m", "vedit.cli", "example"],
                          capture_output=True, text=True, check=True)
    lecture = media.MediaInfo(
        path=Path("lecture.mp4"), width=1920, height=1080, fps=Fraction(30),
        pix_fmt="yuv420p", duration=1200.0, has_audio=True,
        sample_rate=48000, channels=2,
    )
    spec_mod.from_dict(json.loads(proc.stdout), lecture, origin="example")
