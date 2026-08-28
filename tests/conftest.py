"""Synthetic media fixtures, built once per test session."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from util import FPS, SOURCE_SECONDS, ffmpeg

for _tool in ("ffmpeg", "ffprobe"):
    if shutil.which(_tool) is None:
        pytest.skip(f"{_tool} is not installed", allow_module_level=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """A 30s clip with audio, a 30s clip without, and an awkward image.

    The image is deliberately square, larger than the video and carries an alpha
    channel, so the letterboxing and flattening paths are exercised rather than
    a conveniently pre-matched file.
    """
    directory = tmp_path_factory.mktemp("media")

    video = directory / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={FPS}:duration={SOURCE_SECONDS}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={SOURCE_SECONDS}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ac", "1", "-shortest", str(video))

    silent = directory / "silent.mp4"
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={FPS}:duration={SOURCE_SECONDS}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(silent))

    image = directory / "slide.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=darkgreen@0.6:s=900x900,format=rgba",
           "-frames:v", "1", str(image))

    return SimpleNamespace(video=video, silent=silent, image=image)
