"""Floating text burned over the video.

The label sits in a translucent dark box, so its presence shows up as a drop in
brightness in the band where it is drawn. Two rules keep the measurement honest:
sample only the band the box actually covers (a wider band dilutes the signal),
and compare the same timestamp across a labelled and an unlabelled render (the
test background moves, so two timestamps of one render measure the background).
"""

from __future__ import annotations

import pytest

from util import duration, region_mean, render, resolution


def _band(path, at, position):
    """Mean brightness of the strip a label of that position is drawn into."""
    width, height = resolution(path)
    top, bottom = {"top": (20, 55), "bottom": (height - 55, height - 20)}[position]
    return region_mean(path, at, width // 2 - 70, top, width // 2 + 70, bottom)


def test_a_speed_label_appears_only_over_its_own_stretch(tmp_path, media):
    """Source 10-20s at 2x lands at output 10-15s, so 12s is inside and 17s is past it."""
    ramp = {"range": ["0:10", "0:20"], "factor": 2}
    plain = render(tmp_path, media.video, {"speed": [ramp]}, name="plain.mp4")
    labelled = render(tmp_path, media.video,
                      {"speed": [dict(ramp, label="2x - waiting")]}, name="labelled.mp4")

    inside = _band(plain, 12.0, "bottom") - _band(labelled, 12.0, "bottom")
    after = abs(_band(plain, 17.0, "bottom") - _band(labelled, 17.0, "bottom"))
    assert inside > 20, f"no label box inside the window (difference {inside:.1f})"
    assert after < 3, f"the picture changed after the window closed ({after:.1f})"


def test_a_label_does_not_change_the_duration(tmp_path, media):
    ramp = {"range": ["0:10", "0:20"], "factor": 2}
    plain = render(tmp_path, media.video, {"speed": [ramp]}, name="a.mp4")
    labelled = render(tmp_path, media.video,
                      {"speed": [dict(ramp, label="2x")]}, name="b.mp4")
    assert duration(labelled) == pytest.approx(duration(plain), abs=0.02)


def test_position_moves_the_text(tmp_path, media):
    def spec(where):
        return {"text": [{"range": ["0:05", "0:15"], "text": "caption", "position": where}]}

    top = render(tmp_path, media.video, spec("top"), name="top.mp4")
    bottom = render(tmp_path, media.video, spec("bottom"), name="bottom.mp4")

    assert _band(top, 10.0, "top") < _band(bottom, 10.0, "top")
    assert _band(bottom, 10.0, "bottom") < _band(top, 10.0, "bottom")


def test_a_caption_lands_in_its_own_window(tmp_path, media):
    """With no cuts or ramps, source time and output time coincide."""
    plain = render(tmp_path, media.video, {}, name="none.mp4")
    out = render(tmp_path, media.video,
                 {"text": [{"range": ["0:20", "0:25"], "text": "late caption"}]},
                 name="late.mp4")
    inside = _band(plain, 22.0, "bottom") - _band(out, 22.0, "bottom")
    before = abs(_band(plain, 5.0, "bottom") - _band(out, 5.0, "bottom"))
    assert inside > 20, f"the caption is missing inside its window ({inside:.1f})"
    assert before < 3, f"the picture changed before the window opened ({before:.1f})"


def test_a_caption_spanning_a_slide_is_not_drawn_over_the_card(tmp_path, media):
    """A caption whose range contains a card should pause for it, not cover it.

    Both renders carry a caption, so both re-encode; only the span differs. If the
    window were not split, the spanning render would put text on the card.
    """
    slide = {"at": "0:12", "text": "Section two", "seconds": 3}
    spanning = render(tmp_path, media.video,
                      {"slides": [slide],
                       "text": [{"range": ["0:05", "0:20"], "text": "long caption"}]},
                      name="spanning.mp4")
    stopping = render(tmp_path, media.video,
                      {"slides": [slide],
                       "text": [{"range": ["0:05", "0:10"], "text": "long caption"}]},
                      name="stopping.mp4")
    # The card runs from output 12s to 15s; sample its middle.
    difference = abs(_band(spanning, 13.5, "bottom") - _band(stopping, 13.5, "bottom"))
    assert difference < 3, f"the caption bled onto the card ({difference:.1f})"
