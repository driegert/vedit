"""Helpers for driving vedit and measuring what it produced."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

FPS = 30
SOURCE_SECONDS = 30


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def _probe(path: Path, *entries: str) -> str:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", *entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def duration(path: Path) -> float:
    return float(_probe(path, "-show_entries", "format=duration"))


def frames(path: Path) -> int:
    return int(_probe(path, "-count_frames", "-select_streams", "v:0",
                      "-show_entries", "stream=nb_read_frames"))


def resolution(path: Path) -> tuple[int, int]:
    width, height = _probe(path, "-select_streams", "v:0",
                           "-show_entries", "stream=width,height").split(",")
    return int(width), int(height)


def has_audio(path: Path) -> bool:
    return bool(_probe(path, "-select_streams", "a", "-show_entries", "stream=index"))


def pixel(path: Path, at: float, x: int, y: int) -> tuple[int, int, int]:
    """The RGB value of one pixel of the frame shown at `at` seconds."""
    width, _ = resolution(path)
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    )
    offset = (y * width + x) * 3
    return tuple(proc.stdout[offset:offset + 3])


def frame_bytes(path: Path, at: float) -> bytes:
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    )
    return proc.stdout


def loudness(path: Path) -> float:
    """Integrated loudness in LUFS, via a loudnorm measurement pass."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path),
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    blob = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.DOTALL)
    assert blob, f"no loudnorm summary in ffmpeg output:\n{proc.stderr[-800:]}"
    return float(json.loads(blob.group(0))["input_i"])


def chapters_of(path: Path) -> list[tuple[float, str]]:
    """(start_seconds, title) for each embedded chapter."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return [(float(c["start_time"]), c.get("tags", {}).get("title", ""))
            for c in json.loads(proc.stdout).get("chapters", [])]


def region_mean(path: Path, at: float, x0: int, y0: int, x1: int, y1: int) -> float:
    """Mean brightness of a rectangle, averaged over all three channels.

    A single pixel is too fragile for detecting a text box -- it can land on a
    white glyph instead of the dark background behind it.
    """
    width, _ = resolution(path)
    raw = frame_bytes(path, at)
    total = count = 0
    for y in range(y0, y1):
        row = (y * width + x0) * 3
        for value in raw[row:row + (x1 - x0) * 3]:
            total += value
            count += 1
    return total / max(count, 1)


def run_vedit(workdir: Path, source: Path, spec: dict, out: Path,
              *extra: str) -> subprocess.CompletedProcess:
    """Invoke the CLI from the working tree, so tests never hit a stale install."""
    spec_file = workdir / "spec.json"
    spec_file.write_text(json.dumps(spec))
    return subprocess.run(
        [sys.executable, "-m", "vedit.cli", "apply", str(source), str(spec_file),
         "-o", str(out), "-q", *extra],
        capture_output=True, text=True,
    )


def render(workdir: Path, source: Path, spec: dict, name: str = "out.mp4") -> Path:
    """Render and assert it succeeded, returning the output path."""
    out = workdir / name
    proc = run_vedit(workdir, source, spec, out)
    assert proc.returncode == 0, f"vedit failed:\n{proc.stdout}\n{proc.stderr}"
    assert out.exists(), "vedit reported success but wrote no file"
    return out
