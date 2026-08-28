"""Transcription: the parts that do not need the network.

The HTTP call is deliberately not exercised here -- it would tie the suite to one
machine on one network. Window planning and formatting are where the mistakes
that matter live, and both are pure.
"""

from __future__ import annotations

import json

import pytest

from vedit import VeditError
from vedit.transcribe import format_transcript, pieces_from_response, window_starts

from util import duration


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


def test_segments_are_stamped_at_their_own_start():
    body = '{"text": "a b", "segments": [{"start": 0.0, "end": 3.1, "text": " a "}, ' \
           '{"start": 3.7, "end": 10.9, "text": "b"}]}'
    assert pieces_from_response(body) == [(0.0, "a"), (3.7, "b")]


def test_a_text_only_reply_falls_back_to_the_window_start():
    assert pieces_from_response('{"text": " old server "}') == [(0.0, "old server")]
    assert pieces_from_response("plain text") == [(0.0, "plain text")]
    assert pieces_from_response('{"text": ""}') == []
    assert pieces_from_response('{"segments": []}') == []


@pytest.mark.parametrize("body", [
    '{"error": "CUDA out of memory"}',
    '{"segments": [{"start": 1.0, "text": null}]}',
    '{"segments": [{"text": "no start"}]}',
    '{"segments": [{"start": "soon", "text": "x"}]}',
    '{"segments": [{"start": true, "text": "x"}]}',
    '{"segments": [{"start": NaN, "text": "x"}]}',
    '{"segments": [1, 2]}',
    '{"status": "ok"}',
    '[1, 2, 3]',
    '{"text": 5}',
])
def test_error_shaped_and_malformed_replies_fail_closed(body):
    with pytest.raises(VeditError):
        pieces_from_response(body)


def test_transcribe_offsets_each_window_and_reads_the_real_file(media, monkeypatch):
    """The whole path except the HTTP call: windows are cut from the fixture with
    ffmpeg, and each window's segment starts land in source time."""
    import vedit.transcribe as tr
    sent = []

    def fake_post(url, audio, timeout):
        sent.append((len(audio), timeout))
        n = len(sent)
        return json.dumps({"text": "...", "segments": [
            {"start": 0.5, "end": 2.0, "text": f" window {n} first "},
            {"start": 3.25, "end": 4.0, "text": f"window {n} second"}]})

    monkeypatch.setattr(tr, "_post_audio", fake_post)
    text = tr.transcribe(media.video, window=7.0, timeout=5.0)
    assert len(sent) == len(tr.window_starts(duration(media.video), 7.0))
    assert all(size > 0 for size, _ in sent), "an empty chunk was posted"
    assert all(t >= 5.0 for _, t in sent)
    assert text.splitlines()[0] == "[0:00] window 1 first"
    assert "[0:03] window 1 second" in text
    assert "[0:07] window 2 first" in text
    assert "[0:10] window 2 second" in text


def test_transcribe_propagates_a_server_error(media, monkeypatch):
    import vedit.transcribe as tr
    monkeypatch.setattr(tr, "_post_audio", lambda *a: '{"error": "model failed to load"}')
    with pytest.raises(VeditError, match="model failed to load"):
        tr.transcribe(media.video, window=7.0)
