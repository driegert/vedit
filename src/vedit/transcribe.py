"""Timestamped transcription, for proposing chapters.

The faster-whisper server on lilripper returns text only -- it discards
faster-whisper's own segment timings, whatever `response_format` is asked for.
So the timing is recovered by cutting the audio into fixed windows and
transcribing each: the start of every window is known, which gives marks at
window granularity. That is coarse, but chapters want minute-level marks
anyway, and it is roughly 30x faster than real time.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import VeditError
from .media import ffmpeg, probe

DEFAULT_URL = os.environ.get(
    "VEDIT_STT_URL", "http://lilripper:8552/v1/audio/transcriptions"
)
DEFAULT_WINDOW = 120.0
WORKERS = 3          # gentle on a machine that is also serving models


def window_starts(duration: float, window: float) -> list[float]:
    """The start of each fixed window covering `duration`."""
    if duration <= 0:
        raise VeditError("cannot transcribe a file with no duration")
    if window <= 0:
        raise VeditError("the window length must be greater than 0")
    starts, at = [], 0.0
    while at < duration - 1e-6:
        starts.append(at)
        at += window
    return starts or [0.0]


def format_transcript(pieces: list[tuple[float, str]]) -> str:
    """"[m:ss] text" blocks, in source time -- the same clock chapters[] uses."""
    lines = []
    for start, text in sorted(pieces):
        minutes, seconds = divmod(int(start), 60)
        hours, minutes = divmod(minutes, 60)
        stamp = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
        lines.append(f"[{stamp}] {text.strip()}")
    return "\n\n".join(lines) + "\n"


def _post_audio(url: str, audio: bytes, timeout: float) -> str:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("response_format", "text"), ("language", "en")):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="chunk.mp3"\r\nContent-Type: audio/mpeg\r\n\r\n'.encode()
        + audio + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        url, data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise VeditError(
            f"could not reach the transcription service at {url} ({exc.reason}). "
            f"Set VEDIT_STT_URL if it lives somewhere else."
        ) from None
    try:
        return json.loads(body).get("text", "")
    except json.JSONDecodeError:
        return body


def transcribe(video: str | Path, *, url: str = DEFAULT_URL,
               window: float = DEFAULT_WINDOW, workdir: Path | None = None,
               timeout: float = 300.0) -> str:
    """A timestamped transcript of `video`, in source time."""
    info = probe(video)
    if not info.has_audio:
        raise VeditError(f"{info.path.name} has no audio stream to transcribe")

    import tempfile
    with tempfile.TemporaryDirectory(prefix="vedit-stt-") as tmp:
        scratch = Path(workdir or tmp)

        def one(start: float) -> tuple[float, str]:
            chunk = scratch / f"chunk{int(start):07d}.mp3"
            subprocess.run(
                [ffmpeg(), "-y", "-v", "error", "-ss", f"{start}", "-t", f"{window}",
                 "-i", str(info.path), "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "libmp3lame", "-q:a", "5", str(chunk)],
                check=True,
            )
            text = _post_audio(url, chunk.read_bytes(), timeout)
            chunk.unlink(missing_ok=True)
            return start, text

        starts = window_starts(info.duration, window)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            pieces = list(pool.map(one, starts))

    return format_transcript(pieces)
