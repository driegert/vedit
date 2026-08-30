"""Paste-ready HTML player snippets from a rendered video's embedded chapters.

The output is a fragment (no <html> skeleton) meant for an LMS rich-content
editor — verified against Blackboard Ultra 2026-08-30, which passes <link>,
<script type="module">, and custom elements through its sanitizer. The player
is Vidstack, loaded from jsDelivr at a pinned version: the package's own chunk
imports are absolute pinned jsDelivr URLs, so the version below is the whole
dependency and an unpinned "latest" could silently break every pasted item.

Chapters ride inside the snippet as a base64 data: URI WebVTT track — nothing
to host beside the video, and no cross-origin fetch for the browser to block.
Only the video itself is external, so the host just needs HTTPS + Range.
"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path

from . import VeditError
from . import media

# The version cdn.vidstack.io itself serves as of 2026-08-30. Bump deliberately,
# never to a floating tag: snippets pasted into course items are immortal.
ASSETS_BASE = "https://cdn.jsdelivr.net/npm/@vidstack/cdn@1.15.6"


def read_chapters(path: str | Path) -> list[tuple[float, float, str]]:
    """(start, end, title) per embedded chapter, in seconds."""
    path = Path(path)
    if not path.exists():
        raise VeditError(f"input file does not exist: {path}")
    proc = media.run(
        [media.ffprobe(), "-v", "error", "-print_format", "json",
         "-show_chapters", str(path)],
        what="ffprobe (chapters)",
    )
    out = []
    for c in json.loads(proc.stdout).get("chapters", []):
        title = c.get("tags", {}).get("title", "")
        out.append((float(c["start_time"]), float(c["end_time"]), title))
    return out


def _timestamp(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def _cue_text(title: str, ordinal: int) -> str:
    """One VTT cue line: never empty, never able to break the cue grammar."""
    flat = " ".join(title.split())            # newlines would end the cue
    flat = flat.replace("-->", "→")      # a literal --> reads as a timing line
    if not flat:
        flat = f"Chapter {ordinal}"
    return flat.replace("&", "&amp;").replace("<", "&lt;")


def vtt(chapters: list[tuple[float, float, str]]) -> str:
    lines = ["WEBVTT", ""]
    for i, (start, end, title) in enumerate(chapters, start=1):
        lines.append(f"{_timestamp(start)} --> {_timestamp(end)}")
        lines.append(_cue_text(title, i))
        lines.append("")
    return "\n".join(lines)


def build(video_url: str, chapters: list[tuple[float, float, str]],
          assets: str = ASSETS_BASE, max_width: int = 960) -> str:
    assets = assets.rstrip("/")
    track = ""
    if chapters:
        encoded = base64.b64encode(vtt(chapters).encode("utf-8")).decode("ascii")
        track = ('\n    <track kind="chapters" srclang="en" default\n'
                 f'           src="data:text/vtt;base64,{encoded}">')
    return f"""\
<link rel="stylesheet" href="{html.escape(assets)}/styles/player/default/theme.css">
<link rel="stylesheet" href="{html.escape(assets)}/styles/player/default/layouts/video.css">
<script src="{html.escape(assets)}/player.js" type="module"></script>
<media-player src="{html.escape(video_url)}" playsinline
              style="max-width:{max_width}px;display:block;">
  <media-provider>{track}
  </media-provider>
  <media-video-layout></media-video-layout>
</media-player>
"""
