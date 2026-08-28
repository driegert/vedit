"""Turning an EditSpec into a finished video file.

Strategy, and why it is shaped this way:

* auto-editor does the cuts and speed ramps. Its ranges are all SOURCE-relative,
  so earlier cuts never shift later timestamps.
* auto-editor cannot composite two different source files into one timeline, so
  slides are rendered separately and joined with ffmpeg's concat demuxer.
* Segments are written with PCM audio. Concatenating AAC segments accumulates a
  few milliseconds of priming padding at every junction, which shows up as
  progressive audio drift; PCM junctions are sample-exact, and the audio is
  encoded exactly once at the end.
* The video is stream-copied by the concat step, so it is encoded only once.
"""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from pathlib import Path

from . import VeditError
from .media import MediaInfo, auto_editor, duration_of, ffmpeg, run
from .spec import EditSpec, Slide

# auto-editor understands only a small set of encoder flags; -preset and -crf are
# not among them and would be misparsed as input filenames.
AE_ENCODE = ["-c:v", "libx264"]
FF_ENCODE = ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]


def _log(message: str, *, quiet: bool) -> None:
    if not quiet:
        print(message, file=sys.stderr, flush=True)


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort and coalesce frame ranges into a non-overlapping list."""
    merged: list[tuple[int, int]] = []
    for start, stop in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    return merged


def _subtract(start: int, stop: int, holes: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[start, stop) with each (already merged) hole removed."""
    pieces: list[tuple[int, int]] = []
    cursor = start
    for hole_start, hole_stop in holes:
        if hole_stop <= cursor or hole_start >= stop:
            continue
        if hole_start > cursor:
            pieces.append((cursor, min(hole_start, stop)))
        cursor = max(cursor, hole_stop)
        if cursor >= stop:
            break
    if cursor < stop:
        pieces.append((cursor, stop))
    return [(a, b) for a, b in pieces if b > a]


def _cut_frames(spec: EditSpec, info: MediaInfo,
                span: tuple[int, int] | None = None) -> list[tuple[int, int]]:
    low, high = span if span is not None else (0, info.total_frames)
    ranges = []
    for start, stop in spec.cuts:
        first, last = max(info.frame_at(start), low), min(info.frame_at(stop), high)
        if last > first:
            ranges.append((first, last))
    return ranges


def _surviving_frames(spec: EditSpec, info: MediaInfo, start: int, stop: int) -> int:
    """How many frames of [start, stop) the user's cuts leave behind."""
    remaining = stop - start
    for cut_start, cut_stop in _merge(_cut_frames(spec, info)):
        remaining -= max(0, min(cut_stop, stop) - max(cut_start, start))
    return max(0, remaining)


def _edit_flags(spec: EditSpec, info: MediaInfo,
                span: tuple[int, int] | None = None,
                extra_cuts: list[tuple[int, int]] | None = None) -> list[str]:
    """auto-editor flags for the user's cuts and speed ramps, clipped to `span`.

    Three auto-editor behaviours make this fiddlier than it looks:

    * --when-silent nil is essential. Without it auto-editor also removes every
      silent passage, on top of the edits actually requested.
    * The default --edit audio method reads the audio stream and fails outright on
      a video that has none.
    * --set-speed takes precedence over --cut-out: a range that is cut away comes
      back at the given speed if a speed ramp also covers it. So every speed ramp
      has the cuts subtracted from it before being emitted, which makes a cut win
      over a speed ramp on the same footage, and stops a ramp belonging to another
      span from resurrecting footage this span deliberately removed.
    """
    low, high = span if span is not None else (0, info.total_frames)
    args = ["--when-silent", "nil"]
    if not info.has_audio:
        args += ["--edit", "none"]

    cuts = _cut_frames(spec, info, span) + list(extra_cuts or [])
    for first, last in cuts:
        args += ["--cut-out", f"{first},{last}"]

    holes = _merge(cuts)
    # Note the argument order: FACTOR comes first, then the range.
    for factor, start, stop in spec.speeds:
        first, last = max(info.frame_at(start), low), min(info.frame_at(stop), high)
        if last <= first:
            continue
        for piece_start, piece_stop in _subtract(first, last, holes):
            args += ["--set-speed", f"{factor:g},{piece_start},{piece_stop}"]
    return args


def _render_span(info: MediaInfo, spec: EditSpec, start: int, stop: int,
                 dest: Path, *, quiet: bool) -> Path | None:
    """Render source frames [start, stop) with the user's edits applied."""
    if _surviving_frames(spec, info, start, stop) == 0:
        _log(f"  (nothing survives the cuts between "
             f"{start / float(info.fps):.2f}s and {stop / float(info.fps):.2f}s)", quiet=quiet)
        return None

    extra = []
    if start > 0:
        extra.append((0, start))
    if stop < info.total_frames:
        extra.append((stop, info.total_frames))

    cmd = [auto_editor(), str(info.path), *_edit_flags(spec, info, (start, stop), extra),
           *AE_ENCODE, *(["-c:a", "pcm_s16le"] if info.has_audio else ["-an"]),
           "-o", str(dest), "--no-open", "--progress", "none"]
    run(cmd, what=f"rendering {start / float(info.fps):.2f}s-{stop / float(info.fps):.2f}s")
    if duration_of(dest) <= 0:
        raise VeditError(
            f"auto-editor wrote an empty segment for "
            f"{start / float(info.fps):.2f}s-{stop / float(info.fps):.2f}s"
        )
    return dest


def _render_slide(slide: Slide, ref: MediaInfo, dest: Path, workdir: Path) -> Path:
    """Render one slide as a clip matching the reference stream parameters."""
    fps = float(ref.fps)
    if slide.image is not None:
        inputs = ["-loop", "1", "-i", str(slide.image)]
        filters = (
            f"scale={ref.width}:{ref.height}:force_original_aspect_ratio=decrease,"
            f"pad={ref.width}:{ref.height}:(ow-iw)/2:(oh-ih)/2:{slide.background},"
            f"format={ref.pix_fmt}"
        )
    else:
        # A text file sidesteps drawtext's escaping rules entirely, so slide text may
        # contain colons, quotes and newlines. expansion=none is still required, or
        # drawtext would evaluate %{...} sequences in the text as its own directives.
        text_file = workdir / f"{dest.stem}.txt"
        text_file.write_text(slide.text or "")
        size = slide.font_size or max(24, ref.height // 10)
        inputs = ["-f", "lavfi", "-i",
                  f"color=c={slide.background}:s={ref.width}x{ref.height}"
                  f":r={fps:g}:d={slide.seconds}"]
        filters = (
            f"drawtext=textfile={text_file}:expansion=none:fontsize={size}"
            f":fontcolor={slide.color}:x=(w-tw)/2:y=(h-th)/2:line_spacing=12,"
            f"format={ref.pix_fmt}"
        )

    cmd = [ffmpeg(), "-y", "-v", "error", *inputs]
    if ref.has_audio:
        cmd += ["-f", "lavfi", "-i",
                f"anullsrc=r={ref.sample_rate}:cl={ref.channel_layout}:d={slide.seconds}"]
    cmd += ["-vf", filters, "-t", f"{slide.seconds}", "-r", f"{fps:g}",
            *FF_ENCODE, "-pix_fmt", ref.pix_fmt]
    if ref.has_audio:
        cmd += ["-c:a", "pcm_s16le", "-ar", str(ref.sample_rate), "-ac", str(ref.channels)]
    else:
        cmd += ["-an"]
    cmd += ["-shortest", str(dest)]
    run(cmd, what=f"rendering slide ({slide.label})")
    return dest


def _concat(pieces: list[Path], out: Path, ref: MediaInfo, workdir: Path) -> None:
    listing = workdir / "concat.txt"
    # The demuxer treats a backslash and a quote as escapes inside file '...'.
    listing.write_text("".join(
        "file '{}'\n".format(str(p.resolve()).replace("\\", "\\\\").replace("'", r"'\''"))
        for p in pieces
    ))
    cmd = [ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
           "-i", str(listing), "-c:v", "copy"]
    if ref.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", str(ref.sample_rate)]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(out)]
    run(cmd, what="joining segments")


def plan(spec: EditSpec, info: MediaInfo) -> list[str]:
    """A human-readable description of what would happen."""
    lines = [
        f"source     {info.path.name}  {info.width}x{info.height} "
        f"@ {float(info.fps):g}fps  {info.duration:.2f}s "
        f"({'audio ' + str(info.channels) + 'ch' if info.has_audio else 'no audio'})"
    ]
    for start, stop in spec.cuts:
        lines.append(f"cut        {start:8.2f}s -> {stop:8.2f}s   (removes {stop - start:.2f}s)")
    for factor, start, stop in spec.speeds:
        span = stop - start
        lines.append(
            f"speed {factor:>5g}x {start:8.2f}s -> {stop:8.2f}s   "
            f"({span:.2f}s becomes {span / factor:.2f}s)"
        )
    for slide in spec.slides:
        lines.append(f"slide      {slide.at:8.2f}s   {slide.seconds:g}s  {slide.label}")
    if not (spec.cuts or spec.speeds or spec.slides):
        lines.append("(the spec requests no changes)")

    # Cuts win over speed ramps, so only the un-cut part of a ramp saves any time.
    holes = _merge(_cut_frames(spec, info))
    removed = sum(stop - start for start, stop in holes) / float(info.fps)
    saved = 0.0
    for factor, start, stop in spec.speeds:
        for first, last in _subtract(info.frame_at(start), info.frame_at(stop), holes):
            span = (last - first) / float(info.fps)
            saved += span - span / factor
    added = sum(s.seconds for s in spec.slides)
    lines.append(
        f"estimate   {info.duration:.2f}s - {removed:.2f}s cut - {saved:.2f}s sped "
        f"+ {added:.2f}s slides = {info.duration - removed - saved + added:.2f}s"
    )
    return lines


def render(info: MediaInfo, spec: EditSpec, out: Path, *,
           quiet: bool = False, dry_run: bool = False) -> Path:
    out = Path(out)
    if dry_run:
        return out

    if out.resolve() == info.path.resolve():
        raise VeditError(
            f"the output path is the same file as the source ({out}). "
            f"Choose a different name so the original is not overwritten."
        )
    if not spec.slides and _surviving_frames(spec, info, 0, info.total_frames) == 0:
        raise VeditError("the spec removes the entire video; there would be nothing left to write")

    out.parent.mkdir(parents=True, exist_ok=True)
    # Stage beside the destination, keeping the extension so the container is still
    # inferred, then move into place only once the render has fully succeeded.
    staging = out.parent / f".vedit-{out.name}"
    try:
        _render_to(info, spec, staging, quiet=quiet)
        os.replace(staging, out)
    finally:
        staging.unlink(missing_ok=True)
    return out


def _render_to(info: MediaInfo, spec: EditSpec, out: Path, *, quiet: bool) -> None:
    # Without slides there is nothing to join, so let auto-editor write the file.
    if not spec.slides:
        _log("rendering (cuts and speed only)...", quiet=quiet)
        run([auto_editor(), str(info.path), *_edit_flags(spec, info), *AE_ENCODE,
             "-o", str(out), "--no-open", "--progress", "none"], what="rendering")
        return

    with tempfile.TemporaryDirectory(prefix="vedit-") as tmp:
        workdir = Path(tmp)
        points = sorted({info.frame_at(s.at) for s in spec.slides})

        pieces: list[Path] = []
        index = 0
        previous = 0
        for point in sorted({*points, info.total_frames}):
            if point > previous:
                index += 1
                _log(f"rendering segment {index} "
                     f"({previous / float(info.fps):.2f}s - {point / float(info.fps):.2f}s)...",
                     quiet=quiet)
                piece = _render_span(info, spec, previous, point,
                                     workdir / f"seg{index:03d}.mkv", quiet=quiet)
                if piece is not None:
                    pieces.append(piece)
            for number, slide in enumerate(s for s in spec.slides
                                           if info.frame_at(s.at) == point):
                _log(f"rendering slide at {slide.at:.2f}s ({slide.label})...", quiet=quiet)
                pieces.append(_render_slide(
                    slide, info, workdir / f"slide{point:09d}_{number}.mkv", workdir))
            previous = point

        if not pieces:
            raise VeditError("the spec removed the entire video; nothing left to write")

        _log(f"joining {len(pieces)} pieces...", quiet=quiet)
        _concat(pieces, out, info, workdir)


def describe_commands(spec: EditSpec, info: MediaInfo) -> str:
    """The auto-editor invocation for the no-slides case, for --dry-run output."""
    cmd = ["auto-editor", info.path.name, *_edit_flags(spec, info), "-o", "OUTPUT"]
    return " ".join(shlex.quote(part) for part in cmd)
