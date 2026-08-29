"""Locating the external tools, and probing media files."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from . import VeditError


def _tool(name: str) -> str:
    """Find a helper binary, preferring the one installed alongside us."""
    local = Path(sys.prefix) / "bin" / name
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if found is None:
        raise VeditError(
            f"required program {name!r} was not found on PATH. "
            f"Install it with: uv tool install vedit  (auto-editor comes with it), "
            f"and ensure ffmpeg/ffprobe are installed system-wide."
        )
    return found


def auto_editor() -> str:
    return _tool("auto-editor")


def ffmpeg() -> str:
    return _tool("ffmpeg")


def ffprobe() -> str:
    return _tool("ffprobe")


def run(cmd: list[str], *, what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = "\n    ".join(tail[-6:]) or "(no output)"
        raise VeditError(f"{what} failed:\n    {detail}")
    return proc


@dataclass
class MediaInfo:
    path: Path
    width: int
    height: int
    fps: Fraction
    pix_fmt: str
    duration: float
    has_audio: bool
    sample_rate: int
    channels: int
    is_image: bool = False      # a single picture (image2 / *_pipe demuxers), not a video

    @property
    def total_frames(self) -> int:
        return round(self.duration * float(self.fps))

    @property
    def channel_layout(self) -> str:
        return {1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels}c")

    def frame_at(self, seconds: float) -> int:
        """Convert a time in seconds to a source frame index, clamped in range."""
        return max(0, min(self.total_frames, round(seconds * float(self.fps))))


def probe(path: str | Path, *, still: bool = False) -> MediaInfo:
    """Probe a video — or, with `still`, a picture too.

    ffprobe reports a JPEG as a 0.04s `image2` clip but gives a PNG, WebP or BMP no
    duration at all; `vedit still` needs both, so `still` lets a missing duration through.
    """
    path = Path(path)
    if not path.exists():
        raise VeditError(f"input file does not exist: {path}")

    proc = run(
        [ffprobe(), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        what=f"probing {path.name}",
    )
    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise VeditError(f"{path.name} has no video stream")

    rate = video.get("r_frame_rate", "0/0")
    fps = Fraction(rate) if rate not in ("0/0", "0/1") else Fraction(30)
    if fps <= 0:
        fps = Fraction(30)

    format_name = str(data.get("format", {}).get("format_name") or "")
    is_image = format_name == "image2" or format_name.endswith("_pipe")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0.0)
    if duration <= 0 and not (still and is_image):
        raise VeditError(f"could not determine the duration of {path.name}")

    return MediaInfo(
        path=path,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=fps,
        pix_fmt=video.get("pix_fmt") or "yuv420p",
        duration=duration,
        has_audio=audio is not None,
        sample_rate=int(audio.get("sample_rate", 48000)) if audio else 48000,
        channels=int(audio.get("channels", 2)) if audio else 2,
        is_image=is_image,
    )


def duration_of(path: str | Path) -> float:
    """Cheap duration lookup; 0.0 when the file is missing or unreadable."""
    path = Path(path)
    if not path.exists():
        return 0.0
    proc = subprocess.run(
        [ffprobe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0
