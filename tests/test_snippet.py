"""The `snippet` subcommand: paste-ready player HTML from embedded chapters."""

from __future__ import annotations

import base64
import re
import subprocess
import sys

from vedit import snippet as snippet_mod

from util import duration, render


def run_snippet(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "vedit.cli", "snippet", *args],
        capture_output=True, text=True,
    )


def decode_track(html: str) -> str:
    m = re.search(r"data:text/vtt;base64,([A-Za-z0-9+/=]+)", html)
    assert m, "no inlined VTT track in the snippet"
    return base64.b64decode(m.group(1)).decode("utf-8")


def test_snippet_inlines_the_embedded_chapters(tmp_path, media):
    out = render(tmp_path, media.video, {
        "chapters": [{"at": "0:00", "title": "Start"}, {"at": "0:10", "title": "Later"}],
    })
    proc = run_snippet(str(out), "--url", "https://videos.example.ca/v/lecture.mp4")
    assert proc.returncode == 0, proc.stderr
    text = decode_track(proc.stdout)
    assert text.startswith("WEBVTT")
    assert "Start" in text and "Later" in text
    assert "00:00:10.000 -->" in text
    # The last cue must end at the video's end, not at some invented time.
    last_end = re.findall(r"--> (\d\d:\d\d:\d\d\.\d\d\d)", text)[-1]
    h, m, s = last_end.split(":")
    assert abs(int(h) * 3600 + int(m) * 60 + float(s) - duration(out)) < 0.1
    assert 'src="https://videos.example.ca/v/lecture.mp4"' in proc.stdout
    assert snippet_mod.ASSETS_BASE in proc.stdout
    assert "cdn.vidstack.io" not in proc.stdout     # only the pinned origin
    assert "@latest" not in proc.stdout


def test_a_video_without_chapters_still_gets_a_player(tmp_path, media):
    proc = run_snippet(str(media.video), "--url", "https://x.example/v.mp4")
    assert proc.returncode == 0, proc.stderr
    assert "<media-player" in proc.stdout
    assert "<track" not in proc.stdout
    assert "no embedded chapters" in proc.stderr


def test_output_file_and_missing_input(tmp_path, media):
    out_html = tmp_path / "snippet.html"
    proc = run_snippet(str(media.video), "--url", "https://x.example/v.mp4",
                       "-o", str(out_html))
    assert proc.returncode == 0 and out_html.exists()
    assert "<media-player" in out_html.read_text()

    proc = run_snippet(str(tmp_path / "nope.mp4"), "--url", "https://x.example/v.mp4")
    assert proc.returncode == 1
    assert "error:" in proc.stderr and "does not exist" in proc.stderr


def test_titles_cannot_break_the_vtt_or_the_html():
    text = snippet_mod.vtt([
        (0.0, 5.0, "A --> B"),
        (5.0, 8.0, "x & <tag>"),
        (8.0, 9.0, "   "),
    ])
    body = [ln for ln in text.splitlines() if "-->" in ln]
    assert len(body) == 3                     # only the three timing lines
    assert "A \u2192 B" in text
    assert "x &amp; &lt;tag>" in text
    assert "Chapter 3" in text

    html = snippet_mod.build("https://x.example/v.mp4?a=1&b=2", [])
    assert "a=1&amp;b=2" in html
