"""Transcription: the parts that do not need the network.

The HTTP call is deliberately not exercised here -- it would tie the suite to one
machine on one network. Window planning and formatting are where the mistakes
that matter live, and both are pure.
"""

from __future__ import annotations

import pytest

from vedit import VeditError
from vedit.transcribe import format_transcript, window_starts


@pytest.mark.parametrize("duration,window,expected", [
    (300.0, 120.0, [0.0, 120.0, 240.0]),
    (240.0, 120.0, [0.0, 120.0]),
    (60.0, 120.0, [0.0]),
    (0.5, 120.0, [0.0]),
    (1339.97, 120.0, [0.0, 120.0, 240.0, 360.0, 480.0, 600.0,
                      720.0, 840.0, 960.0, 1080.0, 1200.0, 1320.0]),
])
def test_windows_cover_the_whole_file_without_overlap(duration, window, expected):
    assert window_starts(duration, window) == expected


def test_a_window_never_starts_past_the_end():
    for duration in (119.0, 120.0, 121.0, 239.0, 240.0, 241.0):
        starts = window_starts(duration, 120.0)
        assert all(start < duration for start in starts), duration


@pytest.mark.parametrize("duration,window", [(0, 120), (-5, 120), (300, 0), (300, -1)])
def test_nonsense_inputs_are_rejected(duration, window):
    with pytest.raises(VeditError):
        window_starts(duration, window)


def test_transcript_is_stamped_in_source_time():
    text = format_transcript([(0.0, "hello"), (120.0, " second window "), (3725.0, "later")])
    assert text.splitlines()[0] == "[0:00] hello"
    assert "[2:00] second window" in text
    assert "[1:02:05] later" in text


def test_pieces_are_ordered_even_if_they_arrive_out_of_order():
    text = format_transcript([(120.0, "second"), (0.0, "first")])
    assert text.index("first") < text.index("second")
