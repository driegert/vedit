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

    quiet = directory / "quiet.mp4"
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={FPS}:duration={SOURCE_SECONDS}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={SOURCE_SECONDS}",
           "-af", "volume=-30dB",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ac", "1", "-shortest", str(quiet))

    # A screen recording's audio: a quiet voice-level tone with a 1ms click at -6 dBFS
    # every two seconds. A plain gain cannot bring the tone up to -16 LUFS without the
    # clicks breaching the true-peak ceiling. The click must stay SHORT: EBU loudness is
    # energy over 400ms blocks, and a 5ms click carries as much energy as the tone
    # around it, so limiting it would lower the measured loudness itself -- which is
    # not what happens on a real recording, where the voice is continuous.
    clicky = directory / "clicky.mp4"
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={FPS}:duration={SOURCE_SECONDS}",
           "-f", "lavfi", "-i",
           f"aevalsrc=0.06*sin(2*PI*440*t)+0.5*lt(mod(t\\,2)\\,0.001):s=48000:d={SOURCE_SECONDS}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ac", "1", "-shortest", str(clicky))

    image = directory / "slide.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=darkgreen@0.6:s=900x900,format=rgba",
           "-frames:v", "1", str(image))

    return SimpleNamespace(video=video, silent=silent, image=image, quiet=quiet, clicky=clicky)
