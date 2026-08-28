"""Timestamped transcription, for proposing chapters and placing screenshots.

The faster-whisper server on lilripper returns `verbose_json` with one entry per
segment (since 2026-08-28; before that it discarded the timings and returned text
only). Each segment carries its own `start`, so the transcript is stamped at
sentence granularity -- close enough to find the moment a step happens on screen.

The whole file goes up in one request by default. Measured on a 19-minute
recording, 120 s windows were no faster (the server handles one request at a
time) and read worse: whisper loses its punctuation context at every cut. The
window only bounds a very long recording to requests the timeout can hold; the
start of every window is known, so an older server that answers with bare
`{"text": ...}` still degrades to one mark per window instead of failing.
"""

from __future__ import annotations

import json
import math
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
DEFAULT_WINDOW = 1800.0   # ~80 s of server time per request at the measured ~23x
WORKERS = 1               # the server handles one request at a time; parallel calls only queue


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


def pieces_from_response(body: str) -> list[tuple[float, str]]:
    """(start, text) pairs from one window's reply, in window-relative time.

    `verbose_json` gives one pair per segment. A reply with no segments -- an
    older server, or `text` output -- becomes a single pair at 0, so the caller
    still gets a mark at the window start. Anything else is an error: an empty
    transcript that looks valid would silently mis-time every chapter and
    screenshot decided from it.
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return [(0.0, body)] if body.strip() else []
    if not isinstance(data, dict):
        raise VeditError(f"unexpected reply from the transcription service: {body[:200]!r}")
    if "error" in data:
        raise VeditError(f"the transcription service reported an error: {data['error']}")
    segments = data.get("segments")
    if isinstance(segments, list):          # empty is a silent window, not an error
        pieces = []
        for i, seg in enumerate(segments):
            text = seg.get("text") if isinstance(seg, dict) else None
            start = seg.get("start") if isinstance(seg, dict) else None
            if not isinstance(text, str) or isinstance(start, bool) \
                    or not isinstance(start, (int, float)) or not math.isfinite(start):
                raise VeditError(f"malformed segment {i} in the transcription reply: {seg!r}"[:300])
            if text.strip():
                pieces.append((max(float(start), 0.0), text.strip()))
        return pieces
    if "text" not in data:
        raise VeditError(f"the transcription reply has neither segments nor text: {body[:200]!r}")
    text = data["text"]
    if not isinstance(text, str):
        raise VeditError(f"the transcription reply's text is not a string: {text!r}"[:300])
    return [(0.0, text.strip())] if text.strip() else []


def format_transcript(pieces: list[tuple[float, str]]) -> str:
    """One "[m:ss] text" line per piece, in source time -- the same clock the spec uses."""
    lines = []
    for start, text in sorted(pieces):
        text = text.strip()
        if not text:
            continue
        minutes, seconds = divmod(int(start), 60)
        hours, minutes = divmod(minutes, 60)
        stamp = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
        lines.append(f"[{stamp}] {text}")
    return "\n".join(lines) + "\n"


def _post_audio(url: str, audio: bytes, timeout: float) -> str:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("response_format", "verbose_json"), ("language", "en")):
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
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()[:300]
        raise VeditError(
            f"the transcription service at {url} answered HTTP {exc.code}"
            + (f": {detail}" if detail else "")
        ) from None
    except urllib.error.URLError as exc:
        raise VeditError(
            f"could not reach the transcription service at {url} ({exc.reason}). "
            f"Set VEDIT_STT_URL if it lives somewhere else."
        ) from None


def transcribe(video: str | Path, *, url: str = DEFAULT_URL,
               window: float = DEFAULT_WINDOW, workdir: Path | None = None,
               timeout: float = 300.0) -> str:
    """A timestamped transcript of `video`, in source time.

    `timeout` is a floor: each request is allowed at least a quarter of real
    time for its window (the measured rate is ~23x), plus a minute for the
    model to load.
    """
    info = probe(video)
    if not info.has_audio:
        raise VeditError(f"{info.path.name} has no audio stream to transcribe")

    import tempfile
    with tempfile.TemporaryDirectory(prefix="vedit-stt-") as tmp:
        scratch = Path(workdir or tmp)

        starts = window_starts(info.duration, window)
        per_request = max(timeout, 60.0 + min(window, info.duration) / 4)

        def one(item: tuple[int, float]) -> list[tuple[float, str]]:
            index, start = item
            chunk = scratch / f"chunk{index:05d}.mp3"
            subprocess.run(
                [ffmpeg(), "-y", "-v", "error", "-ss", f"{start}", "-t", f"{window}",
                 "-i", str(info.path), "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "libmp3lame", "-q:a", "5", str(chunk)],
                check=True,
            )
            body = _post_audio(url, chunk.read_bytes(), per_request)
            chunk.unlink(missing_ok=True)
            return [(start + at, text) for at, text in pieces_from_response(body)]

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            pieces = [p for batch in pool.map(one, enumerate(starts)) for p in batch]

    return format_transcript(pieces)
