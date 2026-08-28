"""Chapter marks, the pasteable list, and the contents card."""

from __future__ import annotations

import pytest

from vedit import media as media_mod, spec as spec_mod
from vedit.render import chapter_lines

from util import chapters_of, duration, render, run_vedit


def _lines(source, raw):
    probed = media_mod.probe(source)
    return chapter_lines(spec_mod.from_dict(raw, probed), probed)


def test_chapters_are_embedded_in_output_time(tmp_path, media):
    """A cut before a chapter must pull that chapter earlier."""
    out = render(tmp_path, media.video, {
        "cuts": [["0:00", "0:05"]],
        "chapters": [{"at": "0:00", "title": "Start"}, {"at": "0:20", "title": "Later"}],
    })
    embedded = chapters_of(out)
    assert [title for _, title in embedded] == ["Start", "Later"]
    assert embedded[1][0] == pytest.approx(15.0, abs=0.05)


def test_the_first_chapter_always_starts_at_zero(tmp_path, media):
    """MP4 chapter tracks cannot begin after zero; the printed list must agree."""
    out = render(tmp_path, media.video,
                 {"chapters": [{"at": "0:10", "title": "Only"}]})
    embedded = chapters_of(out)
    assert embedded[0][0] == pytest.approx(0.0, abs=0.01)
    assert _lines(media.video, {"chapters": [{"at": "0:10", "title": "Only"}]}) == ["0:00 Only"]


def test_the_printed_list_matches_the_embedded_marks(tmp_path, media):
    spec = {"speed": [{"range": ["0:00", "0:10"], "factor": 2}],
            "chapters": [{"at": "0:00", "title": "A"}, {"at": "0:20", "title": "B"}]}
    out = render(tmp_path, media.video, spec)
    embedded = chapters_of(out)
    printed = _lines(media.video, spec)
    assert printed == ["0:00 A", "0:15 B"]
    assert embedded[1][0] == pytest.approx(15.0, abs=0.05)


def test_the_contents_card_adds_time_and_its_own_chapter(tmp_path, media):
    spec = {"chapters": [{"at": "0:00", "title": "Start"}], "toc_card": {"seconds": 4}}
    out = render(tmp_path, media.video, spec)
    assert duration(out) == pytest.approx(34.0, abs=0.05)
    assert [title for _, title in chapters_of(out)] == ["Contents", "Start"]
    assert chapters_of(out)[1][0] == pytest.approx(4.0, abs=0.05)


def test_a_contents_card_without_chapters_is_rejected(tmp_path, media):
    out = tmp_path / "out.mp4"
    proc = run_vedit(tmp_path, media.video, {"toc_card": True}, out)
    assert proc.returncode != 0
    assert "chapters" in proc.stderr
