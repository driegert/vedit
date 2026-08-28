"""Source-to-output time mapping.

This is the arithmetic agents are not asked to do, so it is worth testing on its
own rather than only through rendered output. These are pure and fast.
"""

from __future__ import annotations

import pytest

from vedit import media, spec as spec_mod
from vedit.render import output_time




def mapped(source_video, raw, at, **kwargs):
    probed = media.probe(source_video)
    edit = spec_mod.from_dict(raw, probed)
    return output_time(edit, probed, at, **kwargs)


def test_no_edits_is_the_identity(media):
    assert mapped(media.video, {}, 12.0) == pytest.approx(12.0)


def test_a_cut_before_the_point_pulls_it_earlier(media):
    assert mapped(media.video, {"cuts": [["0:00", "0:05"]]}, 10.0) == pytest.approx(5.0)


def test_a_cut_after_the_point_does_not_move_it(media):
    assert mapped(media.video, {"cuts": [["0:20", "0:25"]]}, 10.0) == pytest.approx(10.0)


def test_a_speed_ramp_compresses_what_precedes(media):
    spec = {"speed": [{"range": ["0:00", "0:10"], "factor": 2}]}
    assert mapped(media.video, spec, 10.0) == pytest.approx(5.0)
    assert mapped(media.video, spec, 20.0) == pytest.approx(15.0)


def test_halfway_into_a_ramp(media):
    spec = {"speed": [{"range": ["0:10", "0:20"], "factor": 2}]}
    assert mapped(media.video, spec, 15.0) == pytest.approx(12.5)


def test_a_slide_pushes_later_footage_back(media):
    spec = {"slides": [{"at": "0:05", "text": "A", "seconds": 3}]}
    assert mapped(media.video, spec, 10.0) == pytest.approx(13.0)


def test_after_slide_selects_which_side_of_a_card(media):
    """Floating text sits over the footage; a chapter mark sits on the card."""
    spec = {"slides": [{"at": "0:05", "text": "A", "seconds": 3}]}
    assert mapped(media.video, spec, 5.0, after_slide=True) == pytest.approx(8.0)
    assert mapped(media.video, spec, 5.0, after_slide=False) == pytest.approx(5.0)


def test_the_contents_card_offsets_everything(media):
    spec = {"chapters": [{"at": 0, "title": "A"}], "toc_card": {"seconds": 4}}
    assert mapped(media.video, spec, 0.0) == pytest.approx(4.0)
    assert mapped(media.video, spec, 10.0) == pytest.approx(14.0)


def test_cuts_speeds_slides_and_card_together(media):
    spec = {
        "cuts": [["0:00", "0:05"]],                              # -5s
        "speed": [{"range": ["0:10", "0:20"], "factor": 2}],     # 10s -> 5s
        "slides": [{"at": "0:08", "text": "A", "seconds": 3}],   # +3s
        "chapters": [{"at": 0, "title": "A"}],
        "toc_card": {"seconds": 4},                              # +4s
    }
    # source 25s: 20s of footage survives, 10s of it halved -> 15s, +3 slide +4 card
    assert mapped(media.video, spec, 25.0) == pytest.approx(22.0)


def test_mapping_matches_the_rendered_duration(tmp_path, media):
    from util import duration, render
    spec = {"cuts": [["0:00", "0:05"]],
            "speed": [{"range": ["0:20", "0:25"], "factor": 2}],
            "slides": [{"at": "0:10", "text": "A", "seconds": 3}]}
    out = render(tmp_path, media.video, spec)
    assert duration(out) == pytest.approx(mapped(media.video, spec, 30.0), abs=0.02)
