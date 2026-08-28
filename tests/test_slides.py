"""Slide rendering: text handling and image fitting."""

from __future__ import annotations

import pytest

from util import frame_bytes, pixel, render, resolution


def test_percent_brace_in_text_is_literal(tmp_path, media):
    """drawtext expands %{...} unless told not to.

    Without expansion=none, "%{pts}" becomes the frame's timestamp, so the slide
    would differ from frame to frame. Two frames from within one static slide
    must therefore be byte-identical.
    """
    out = render(tmp_path, media.video,
                 {"slides": [{"at": 0, "text": "Done: 100%{pts}", "seconds": 3}]})
    assert frame_bytes(out, 0.5) == frame_bytes(out, 2.5)


def test_text_may_contain_colons_and_quotes(tmp_path, media):
    out = render(tmp_path, media.video, {"slides": [
        {"at": 0, "text": "Part 2: Thomson's (1982) \"multitaper\"\nsecond line", "seconds": 2},
    ]})
    assert frame_bytes(out, 0.5) == frame_bytes(out, 1.5)


def test_image_is_letterboxed_not_stretched(tmp_path, media):
    """The fixture image is 900x900 against a 640x360 video.

    Fitting by height gives a 360x360 panel centred horizontally, so the edges
    must be background and the middle must be the image.
    """
    out = render(tmp_path, media.video,
                 {"slides": [{"at": 0, "image": str(media.image), "seconds": 3}]})
    assert resolution(out) == resolution(media.video)

    edge = pixel(out, 1.0, 10, 180)
    centre = pixel(out, 1.0, 320, 180)
    assert max(edge) < 24, f"expected a dark pillarbox at the edge, got {edge}"
    assert centre[1] > centre[0] + 20 and centre[1] > centre[2] + 20, \
        f"expected the green image in the centre, got {centre}"


def test_slide_background_colour_is_applied(tmp_path, media):
    out = render(tmp_path, media.video,
                 {"slides": [{"at": 0, "text": "hi", "background": "#0000ff", "seconds": 2}]})
    corner = pixel(out, 1.0, 5, 5)
    assert corner[2] > 200 and corner[0] < 60, f"expected a blue background, got {corner}"


def test_font_size_is_accepted(tmp_path, media):
    small = render(tmp_path, media.video,
                   {"slides": [{"at": 0, "text": "SIZE", "font_size": 12, "seconds": 2}]},
                   name="small.mp4")
    large = render(tmp_path, media.video,
                   {"slides": [{"at": 0, "text": "SIZE", "font_size": 96, "seconds": 2}]},
                   name="large.mp4")
    assert frame_bytes(small, 1.0) != frame_bytes(large, 1.0)
