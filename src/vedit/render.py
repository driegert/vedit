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

import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from . import VeditError
from .media import MediaInfo, auto_editor, duration_of, ffmpeg, run
from .spec import Audio, EditSpec, Overlay, Slide

# auto-editor understands only a small set of encoder flags; -preset and -crf are
# not among them and would be misparsed as input filenames.
TRUE_PEAK_CEILING = -1.5     # dBTP, the usual broadcast ceiling

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


def output_time(spec: EditSpec, info: MediaInfo, source_seconds: float,
                *, after_slide: bool = True) -> float:
    """Where a SOURCE timestamp lands in the assembled output, in seconds.

    Cuts remove time, speed ramps compress it, and slides and the contents card
    add it. Agents cannot be expected to do this arithmetic, which is the whole
    reason floating text and chapters are specified in source time.

    `after_slide` decides which side of a slide a timestamp sits on: floating
    text belongs over the footage (True), while a chapter mark belongs on the
    card that introduces the section (False).
    """
    fps = float(info.fps)
    target = info.frame_at(source_seconds)
    holes = _merge(_cut_frames(spec, info))
    ramps = sorted((info.frame_at(a), info.frame_at(b), f) for f, a, b in spec.speeds)

    bounds = {0, target}
    for start, stop in holes:
        bounds |= {start, stop}
    for start, stop, _ in ramps:
        bounds |= {start, stop}
    ordered = sorted(p for p in bounds if 0 <= p <= target)

    frames = 0.0
    for low, high in zip(ordered, ordered[1:]):
        middle = (low + high) / 2
        if any(start <= middle < stop for start, stop in holes):
            continue
        factor = next((f for start, stop, f in ramps if start <= middle < stop), 1.0)
        frames += (high - low) / factor

    seconds = frames / fps
    for slide in spec.slides:
        at = info.frame_at(slide.at)
        if at < target or (after_slide and at == target):
            seconds += slide.seconds
    if spec.toc_card is not None:
        seconds += spec.toc_card.seconds
    return seconds


def _chapter_marks(spec: EditSpec, info: MediaInfo) -> list[tuple[float, str]]:
    """Chapter positions in OUTPUT time, shared by the file and the printed list.

    The first chapter always starts at 0. An MP4 chapter track is a text track
    covering the whole timeline and cannot begin after zero -- ffmpeg silently
    pins it, which would make the printed list disagree with the file. Matroska
    would keep the offset, but one honest rule beats two container-specific ones.
    """
    marks = [(output_time(spec, info, c.at, after_slide=False), c.title)
             for c in spec.chapters]
    marks.sort()

    minimum = 1 / float(info.fps)
    total = output_time(spec, info, info.duration)
    # Checked before the first mark is pinned to zero, or pinning would hide a
    # chapter that the edits pushed off the end of the result.
    if marks and marks[-1][0] >= total - minimum:
        raise VeditError(
            f"chapter {marks[-1][1]!r} lands at {marks[-1][0]:.2f}s, at or past the "
            f"end of the {total:.2f}s result; move it earlier."
        )

    if spec.toc_card is not None:
        marks.insert(0, (0.0, spec.toc_card.title))
    elif marks and marks[0][0] > 0:
        marks[0] = (0.0, marks[0][1])

    # A chapter shorter than a frame cannot be written to the file, so it would
    # appear in the printed list and vanish from the video. Say so instead.
    for (start, title), (next_start, next_title) in zip(marks, marks[1:]):
        if next_start - start < minimum:
            raise VeditError(
                f"chapters {title!r} and {next_title!r} both land at "
                f"{start:.2f}s in the finished video, so one of them would be lost. "
                f"They are probably inside a range you also cut."
            )
    return marks


def chapter_lines(spec: EditSpec, info: MediaInfo) -> list[str]:
    """Chapters as "m:ss title" in OUTPUT time, ready to paste into a description."""
    lines = []
    for at, title in _chapter_marks(spec, info):
        hours, rest = divmod(int(at), 3600)
        minutes, seconds = divmod(rest, 60)
        stamp = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
        lines.append(f"{stamp} {title}")
    return lines


def _escape_metadata(value: str) -> str:
    for character in ("\\", "=", ";", "#", "\n"):
        value = value.replace(character, "\\" + character)
    return value


def _write_chapters(spec: EditSpec, info: MediaInfo, total: float, path: Path) -> Path:
    """An ffmetadata file of chapter marks, in output time."""
    marks = _chapter_marks(spec, info)
    lines = [";FFMETADATA1"]
    for index, (start, title) in enumerate(marks):
        stop = marks[index + 1][0] if index + 1 < len(marks) else total
        if stop <= start:
            continue
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(start * 1000)}", f"END={int(stop * 1000)}",
                  f"title={_escape_metadata(title)}"]
    path.write_text("\n".join(lines) + "\n")
    return path


def _overlay_windows(spec: EditSpec, info: MediaInfo,
                     overlay: Overlay) -> list[tuple[float, float]]:
    """The stretches of OUTPUT time a caption should be visible for.

    The start sits after a card at the same moment (the caption belongs over the
    footage), the stop before one (it should not spill onto the next card), and
    any card falling inside the range splits the window rather than being written
    over.
    """
    start = output_time(spec, info, overlay.start, after_slide=True)
    stop = output_time(spec, info, overlay.stop, after_slide=False)
    if stop <= start:
        return []

    windows = [(start, stop)]
    for slide in spec.slides:
        if not overlay.start < slide.at < overlay.stop:
            continue
        card_start = output_time(spec, info, slide.at, after_slide=False)
        card_stop = card_start + slide.seconds
        split = []
        for low, high in windows:
            if card_stop <= low or card_start >= high:
                split.append((low, high))
                continue
            if card_start > low:
                split.append((low, card_start))
            if card_stop < high:
                split.append((card_stop, high))
        windows = split

    minimum = 1 / float(info.fps)
    return [(low, high) for low, high in windows if high - low >= minimum]


def check_timeline(spec: EditSpec, info: MediaInfo) -> None:
    """Validation that needs the source-to-output mapping, so a bad spec fails early."""
    for index, overlay in enumerate(spec.overlays):
        if not _overlay_windows(spec, info, overlay):
            raise VeditError(
                f"the caption {overlay.text.splitlines()[0]!r} covering "
                f"{overlay.start:g}s-{overlay.stop:g}s would never be visible: that "
                f"footage is removed by a cut, or compressed below a single frame."
            )
    _chapter_marks(spec, info)


def _overlay_filters(spec: EditSpec, info: MediaInfo, workdir: Path) -> list[str]:
    """One drawtext per floating label, switched on for its stretches of output time."""
    margin = max(20, info.height // 12)
    default_size = max(18, info.height // 18)
    filters = []
    for index, overlay in enumerate(spec.overlays):
        windows = _overlay_windows(spec, info, overlay)
        if not windows:
            continue
        enable = "+".join(f"between(t,{low:.3f},{high:.3f})" for low, high in windows)
        text_file = workdir / f"overlay{index:03d}.txt"
        text_file.write_text(overlay.text)
        y = {"top": f"{margin}",
             "middle": "(h-th)/2",
             "bottom": f"h-th-{margin}"}[overlay.position]
        filters.append(
            f"drawtext=textfile={text_file}:expansion=none"
            f":fontsize={overlay.font_size or default_size}:fontcolor={overlay.color}"
            f":box=1:boxcolor={overlay.background}:boxborderw={max(6, info.height // 60)}"
            f":x=(w-tw)/2:y={y}:enable='{enable}'"
        )
    return filters


def _toc_slide(spec: EditSpec, info: MediaInfo) -> Slide:
    """The contents card, as an ordinary text slide so it renders like any other."""
    card = spec.toc_card
    # Skip the card's own chapter entry -- it should list the sections, not itself.
    entries = [f"{stamp}  {title}" for stamp, title in
               (line.split(" ", 1) for line in chapter_lines(spec, info)[1:])]
    body = "\n".join([card.title, ""] + entries)
    rows = body.count("\n") + 1
    size = card.font_size or max(14, min(info.height // 14, info.height // (rows + 2)))
    return Slide(at=0.0, seconds=card.seconds, text=body,
                 background=card.background, color=card.color, font_size=size)


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


def _concat(pieces: list[Path], out: Path, ref: MediaInfo, workdir: Path,
            *, lossless_audio: bool = False) -> None:
    listing = workdir / "concat.txt"
    # The demuxer treats a backslash and a quote as escapes inside file '...'.
    listing.write_text("".join(
        "file '{}'\n".format(str(p.resolve()).replace("\\", "\\\\").replace("'", r"'\''"))
        for p in pieces
    ))
    cmd = [ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
           "-i", str(listing), "-c:v", "copy"]
    if not ref.has_audio:
        cmd += ["-an"]
    elif lossless_audio:
        # A finishing pass follows and will encode the audio once; staying on PCM
        # until then avoids a second generation of AAC.
        cmd += ["-c:a", "pcm_s16le", "-ar", str(ref.sample_rate)]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", str(ref.sample_rate)]
    if not lossless_audio:
        cmd += ["-movflags", "+faststart"]
    cmd += [str(out)]
    run(cmd, what="joining segments")


def _measure_loudness(path: Path) -> dict[str, str]:
    """Pass one of EBU R128: measure the assembled audio.

    The analysis target is fixed rather than taken from the spec: the input_*
    figures do not depend on it, and loudnorm rejects an I outside -70..-5, which
    a peak-mode target would be.
    """
    proc = subprocess.run(
        [ffmpeg(), "-hide_banner", "-i", str(path),
         "-af", "loudnorm=I=-24:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    blob = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.DOTALL)
    if not blob:
        raise VeditError(
            "could not measure the audio loudness; ffmpeg printed no loudnorm summary"
        )
    return json.loads(blob.group(0))


def _audio_filter(source: Path, audio: Audio, *, quiet: bool) -> str:
    """The measured normalisation filter for the chosen method.

    loudnorm is used to MEASURE (its gating and true-peak detection are exactly
    what EBU R128 asks for) but not to apply the correction. Applying it with
    loudnorm's own second pass lands about 2dB under target here, because
    loudnorm resamples internally to 192kHz and the level does not survive the
    trip back down to the source rate. A plain linear gain hits the target
    exactly, at the source rate, and preserves the dynamics untouched -- which is
    what a too-quiet lecture recording wants anyway.
    """
    measured = _measure_loudness(source)
    current, peak = float(measured["input_i"]), float(measured["input_tp"])
    if not (math.isfinite(current) and math.isfinite(peak)):
        raise VeditError(
            "the audio has no measurable level, so it cannot be normalised; "
            "it is probably silent. Remove audio.normalize."
        )

    if audio.normalize == "peak":
        gain = audio.target - peak
        _log(f"normalising true peak from {peak:g} to {audio.target:g} dBTP "
             f"({gain:+.1f} dB)...", quiet=quiet)
        return f"volume={gain:.2f}dB"

    gain = audio.target - current

    headroom = TRUE_PEAK_CEILING - peak
    if gain > headroom:
        _log(f"  capping the gain at {headroom:+.1f} dB to keep the true peak under "
             f"{TRUE_PEAK_CEILING:g} dBTP; the result lands at "
             f"{current + headroom:.1f} LUFS rather than {audio.target:g}", quiet=quiet)
        gain = headroom

    _log(f"normalising audio from {current:g} to {current + gain:.1f} LUFS "
         f"({gain:+.1f} dB)...", quiet=quiet)
    return f"volume={gain:.2f}dB"


def _finish(source: Path, out: Path, info: MediaInfo, spec: EditSpec,
            workdir: Path, *, quiet: bool) -> None:
    """Burn in floating text, attach chapters and normalise audio, in one pass.

    Each of these is optional; whatever is not requested is stream-copied, so a
    spec that asks for none of them never reaches here at all.
    """
    filters = _overlay_filters(spec, info, workdir)
    normalize = spec.audio.normalize != "none" and info.has_audio

    cmd = [ffmpeg(), "-y", "-v", "error", "-i", str(source)]
    if spec.chapters:
        cmd += ["-i", str(_write_chapters(spec, info, duration_of(source),
                                          workdir / "chapters.txt"))]

    if filters:
        _log(f"burning in {len(filters)} floating label(s)...", quiet=quiet)
        cmd += ["-vf", ",".join(filters), *FF_ENCODE, "-pix_fmt", info.pix_fmt]
    else:
        cmd += ["-c:v", "copy"]

    if normalize:
        cmd += ["-af", _audio_filter(source, spec.audio, quiet=quiet),
                "-c:a", "aac", "-b:a", "192k", "-ar", str(info.sample_rate)]
    elif info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", str(info.sample_rate)]
    else:
        cmd += ["-an"]

    cmd += ["-map", "0:v:0"]
    if info.has_audio:
        cmd += ["-map", "0:a:0"]
    if spec.chapters:
        # Both are needed: -map_metadata alone leaves chapter selection implicit.
        cmd += ["-map_metadata", "1", "-map_chapters", "1"]
    cmd += ["-movflags", "+faststart", str(out)]
    run(cmd, what="finishing pass")


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
    for overlay in spec.overlays:
        first = overlay.text.splitlines()[0] if overlay.text else ""
        lines.append(f"text       {overlay.start:8.2f}s -> {overlay.stop:8.2f}s   "
                     f"{overlay.position}  {first!r}")
    if spec.toc_card is not None:
        lines.append(f"toc card   {spec.toc_card.seconds:g}s at the start, "
                     f"{len(spec.chapters)} entries")
    for line in chapter_lines(spec, info):
        lines.append(f"chapter    {line}")
    if spec.audio.normalize != "none":
        lines.append(f"audio      normalize {spec.audio.normalize} to "
                     f"{spec.audio.target:g} {'LUFS' if spec.audio.normalize == 'ebu' else 'dBTP'}")
    if not (spec.cuts or spec.speeds or spec.slides or spec.overlays
            or spec.chapters or spec.audio.normalize != "none"):
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
    added += spec.toc_card.seconds if spec.toc_card is not None else 0.0
    # Take the total from the same mapping the chapters use, so the two cannot disagree.
    lines.append(
        f"estimate   {info.duration:.2f}s - {removed:.2f}s cut - {saved:.2f}s sped "
        f"+ {added:.2f}s added = {output_time(spec, info, info.duration):.2f}s"
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
    if (not spec.slides and spec.toc_card is None
            and _surviving_frames(spec, info, 0, info.total_frames) == 0):
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


def _needs_finishing(spec: EditSpec, info: MediaInfo) -> bool:
    return bool(spec.overlays or spec.chapters) or (
        spec.audio.normalize != "none" and info.has_audio)


def _render_to(info: MediaInfo, spec: EditSpec, out: Path, *, quiet: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="vedit-") as tmp:
        workdir = Path(tmp)
        finishing = _needs_finishing(spec, info)
        # Assemble into a lossless intermediate when a finishing pass follows.
        assembled = workdir / "assembled.mkv" if finishing else out

        if not spec.slides and spec.toc_card is None:
            # Nothing to join, so auto-editor can write the assembly directly.
            _log("rendering (cuts and speed only)...", quiet=quiet)
            audio = ["-c:a", "pcm_s16le"] if (finishing and info.has_audio) else []
            if not info.has_audio:
                audio = ["-an"]
            run([auto_editor(), str(info.path), *_edit_flags(spec, info), *AE_ENCODE,
                 *audio, "-o", str(assembled), "--no-open", "--progress", "none"],
                what="rendering")
        else:
            _assemble(info, spec, assembled, workdir, quiet=quiet, lossless=finishing)

        if finishing:
            _finish(assembled, out, info, spec, workdir, quiet=quiet)


def _assemble(info: MediaInfo, spec: EditSpec, out: Path, workdir: Path,
              *, quiet: bool, lossless: bool) -> None:
    """Render every span and slide, then join them."""
    pieces: list[Path] = []

    if spec.toc_card is not None:
        _log("rendering the contents card...", quiet=quiet)
        pieces.append(_render_slide(_toc_slide(spec, info), info,
                                    workdir / "toc.mkv", workdir))

    points = sorted({info.frame_at(s.at) for s in spec.slides})
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
    _concat(pieces, out, info, workdir, lossless_audio=lossless)


def describe_commands(spec: EditSpec, info: MediaInfo) -> str:
    """The auto-editor invocation for the no-slides case, for --dry-run output."""
    cmd = ["auto-editor", info.path.name, *_edit_flags(spec, info), "-o", "OUTPUT"]
    return " ".join(shlex.quote(part) for part in cmd)
