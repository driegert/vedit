"""`vedit sheet`: several moments of a video on one labelled contact sheet.

Choosing the frame for a guide screenshot means comparing candidates — a dialog before
and after a click, a command half typed and fully typed. Looking at them one image at a
time costs a couple of thousand tokens each and the earlier ones are forgotten by the
time the later ones arrive. One sheet with the time stamped on every cell is the same
comparison in one look.
"""

from __future__ import annotations

import math
import shutil
import tempfile
from pathlib import Path

from . import VeditError
from .media import MediaInfo, ffmpeg, probe, run
from .spec import parse_time

MAX_CELLS = 12
DEFAULT_COLS = 3
DEFAULT_WIDTH = 640


def _clock(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}" if rest != int(rest) else f"{int(minutes)}:{int(rest):02d}"


def cell_size(info: MediaInfo, width: int) -> tuple[int, int]:
    height = 2 * round(info.height * width / info.width / 2)
    return width, height


def resolve_times(raw: list, info: MediaInfo) -> list[float]:
    if not raw:
        raise VeditError("give at least one time")
    if len(raw) > MAX_CELLS:
        raise VeditError(f"at most {MAX_CELLS} frames per sheet (got {len(raw)}); split them up")
    times = []
    for i, value in enumerate(raw):
        t = parse_time(value, info, where=f"times[{i}]")
        if t >= info.duration:
            raise VeditError(f"times[{i}]: {value!r} is at or past the end of the "
                             f"{info.duration:.2f}s source")
        times.append(t)
    return times


def command(info: MediaInfo, times: list[float], out: Path, workdir: Path,
            *, cols: int = DEFAULT_COLS, width: int = DEFAULT_WIDTH) -> list[str]:
    cw, ch = cell_size(info, width)
    rows = math.ceil(len(times) / cols)
    size = max(16, round(ch / 18))
    cmd = [ffmpeg(), "-y", "-v", "error"]
    for t in times:
        cmd += ["-ss", f"{max(0.0, t - 0.5 / float(info.fps)):.6f}", "-i", str(info.path)]
    parts = []
    for i, t in enumerate(times):
        label = workdir / f"cell{i:02d}.txt"
        label.write_text(f"{_clock(t)}  ({t:g}s)")
        parts.append(
            f"[{i}:v]trim=end_frame=1,setpts=PTS-STARTPTS,scale={cw}:{ch}:flags=lanczos,"
            f"drawtext=textfile={label}:expansion=none:fontsize={size}:fontcolor=yellow"
            f":box=1:boxcolor=black@0.7:boxborderw=6:x=8:y=8[c{i}]"
        )
    chain = "".join(f"[c{i}]" for i in range(len(times)))
    parts.append(f"{chain}concat=n={len(times)}:v=1:a=0,tile={cols}x{rows}[sheet]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[sheet]", "-frames:v", "1", "-update", "1"]
    if out.suffix.lower() in (".jpg", ".jpeg"):
        cmd += ["-q:v", "3"]
    cmd.append(str(out))
    return cmd


def render(info: MediaInfo, times: list[float], out: Path, *, cols: int = DEFAULT_COLS,
           width: int = DEFAULT_WIDTH) -> Path:
    if out.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        raise VeditError(f"write a .jpg or .png (got {out.name})")
    if info.is_image:
        raise VeditError("a sheet needs a video; this is an image")
    if not 1 <= cols <= 6:
        raise VeditError(f"--cols must be between 1 and 6 (got {cols})")
    if not 160 <= width <= 1920:
        raise VeditError(f"--width must be between 160 and 1920 (got {width})")
    out.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="vedit-sheet-"))
    try:
        run(command(info, times, out, workdir, cols=cols, width=width), what="rendering the sheet")
        cw, ch = cell_size(info, width)
        expected = (cw * cols, ch * math.ceil(len(times) / cols))
        result = probe(out, still=True)
        if (result.width, result.height) != expected:
            raise VeditError(f"the sheet measures {result.width}x{result.height} but should be "
                             f"{expected[0]}x{expected[1]}; this is a bug in vedit")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return out
