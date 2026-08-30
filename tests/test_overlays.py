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


def test_chapter_titles_float_at_each_chapter_start(tmp_path, media):
    """chapter_titles derives one top-positioned overlay per chapter, ~4 s each."""
    chapters = [{"at": "0:05", "title": "Part one"}, {"at": "0:15", "title": "Part two"}]
    plain = render(tmp_path, media.video, {"chapters": chapters}, name="ct-plain.mp4")
    titled = render(tmp_path, media.video,
                    {"chapters": chapters, "chapter_titles": True}, name="ct-titled.mp4")

    first = _band(plain, 6.0, "top") - _band(titled, 6.0, "top")
    between = abs(_band(plain, 12.0, "top") - _band(titled, 12.0, "top"))
    second = _band(plain, 16.0, "top") - _band(titled, 16.0, "top")
    assert first > 20, f"no title at the first chapter start ({first:.1f})"
    assert between < 3, f"a title outlived its window ({between:.1f})"
    assert second > 20, f"no title at the second chapter start ({second:.1f})"
    assert duration(titled) == pytest.approx(duration(plain), abs=0.02), "titles must add no time"


def test_a_chapter_title_is_capped_at_the_next_chapter(tmp_path, media):
    """Close chapters must not stack: the first title ends where the next chapter begins.

    The first title is much wider than the second (drawtext centres its box), so a band
    near the frame's left edge sees only the first title's box. At 10 s an UNCAPPED first
    title (5-15 s) would darken that edge band; a capped one (5-7 s) leaves it alone —
    this fails if the cap line is removed, which the old midline sample did not.
    """
    chapters = [{"at": "0:05", "title": "A very long first chapter title spanning nearly the whole frame width"},
                {"at": "0:07", "title": "Two"}]
    plain = render(tmp_path, media.video, {"chapters": chapters}, name="cap-plain.mp4")
    titled = render(tmp_path, media.video,
                    {"chapters": chapters, "chapter_titles": {"seconds": 10}}, name="cap-titled.mp4")

    def edge(path, at):
        # Inside the wide title's box (it spans nearly the frame), left of the short one's.
        return region_mean(path, at, 40, 24, 150, 52)

    during_first = edge(plain, 6.0) - edge(titled, 6.0)
    at_ten = abs(edge(plain, 10.0) - edge(titled, 10.0))
    mid_ten = _band(plain, 10.0, "top") - _band(titled, 10.0, "top")
    assert during_first > 20, f"wide first title missing at its own start ({during_first:.1f})"
    assert at_ten < 3, f"the first title was not capped at the next chapter ({at_ten:.1f})"
    assert mid_ten > 20, f"second title missing ({mid_ten:.1f})"


def test_a_chapter_inside_a_cut_gets_its_title_after_the_cut(tmp_path, media):
    """A chapter in cut footage snaps to the boundary; its title must too, not fail."""
    spec = {"cuts": [["0:10", "0:20"]],
            "chapters": [{"at": "0:12", "title": "After the cut"}],
            "chapter_titles": True}
    plain = render(tmp_path, media.video, {"cuts": [["0:10", "0:20"]]}, name="cut-plain.mp4")
    titled = render(tmp_path, media.video, spec, name="cut-titled.mp4")
    # Source 20s is output 10s; the title covers output 10-14s.
    inside = _band(plain, 11.0, "top") - _band(titled, 11.0, "top")
    after = abs(_band(plain, 16.0, "top") - _band(titled, 16.0, "top"))
    assert inside > 20, f"no title after the cut boundary ({inside:.1f})"
    assert after < 3, f"the title ran long ({after:.1f})"


def test_chapter_titles_off_and_defaults_forms(tmp_path, media):
    """false is off, {} is all-defaults; both validate (dry runs, no render cost)."""
    from util import run_vedit
    base = {"chapters": [{"at": "0:05", "title": "A"}]}
    for value in (False, {}):
        spec = dict(base, chapter_titles=value)
        proc = run_vedit(tmp_path, media.video, spec, tmp_path / "dry.mp4", "--dry-run")
        assert proc.returncode == 0, f"chapter_titles={value!r}:\n{proc.stderr}"


def test_chapter_titles_take_position_and_seconds(tmp_path, media):
    spec = {"chapters": [{"at": "0:05", "title": "Bottom title"}],
            "chapter_titles": {"position": "bottom", "seconds": 3}}
    plain = render(tmp_path, media.video, {}, name="pos-plain.mp4")
    titled = render(tmp_path, media.video, spec, name="pos-titled.mp4")
    inside = _band(plain, 6.0, "bottom") - _band(titled, 6.0, "bottom")
    after = abs(_band(plain, 9.0, "bottom") - _band(titled, 9.0, "bottom"))
    assert inside > 20, f"no title at the bottom ({inside:.1f})"
    assert after < 3, f"the title ran past its seconds ({after:.1f})"


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
