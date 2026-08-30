"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import VeditError, __version__
from . import media, render, snippet as snippet_mod, spec as spec_mod, still as still_mod, transcribe as transcribe_mod

EXAMPLE = {
    "cuts": [["0:00", "1:30"], ["14:05", "end"]],
    "speed": [{"range": ["5:00", "8:00"], "factor": 5.0,
               "label": "5x - waiting for the download"}],
    "text": [{"range": ["9:00", "9:20"], "text": "note: the menu moved in v4",
              "position": "bottom"}],
    "chapters": [{"at": "0:00", "title": "Introduction"},
                 {"at": "8:00", "title": "Installing the toolchain"}],
    "chapter_titles": True,
    "toc_card": True,
    "audio": {"normalize": "ebu", "target": -16},
}

# Percentages, so the example is valid on any frame size.
STILL_EXAMPLE = {
    "at": "2:29",
    "highlights": [
        {"shape": "box", "x": "61%", "y": "58%", "w": "14%", "h": "7%", "label": "1. Download"},
        {"shape": "ellipse", "x": "15%", "y": "8%", "w": "8%", "h": "14%", "color": "yellow"},
    ],
    "dim": 0.4,
    "pad": 20,
    "crop": {"margin": 80},
    "max_width": 1280,
    "notes": "coordinates are full-frame pixels or percentages, measured with grid: true; "
             "the outline is drawn pad pixels outside them",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vedit",
        description="Edit a video from a declarative JSON spec: cuts, speed ramps, title slides.",
    )
    parser.add_argument("--version", action="version", version=f"vedit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    apply_cmd = sub.add_parser("apply", help="apply an edit spec to a video")
    apply_cmd.add_argument("video", help="the source video file")
    apply_cmd.add_argument("spec", help="path to the JSON spec, or - to read stdin")
    apply_cmd.add_argument("-o", "--output", required=True, help="the file to write")
    apply_cmd.add_argument("--dry-run", action="store_true",
                           help="print the resolved plan and exit without rendering")
    apply_cmd.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")

    probe_cmd = sub.add_parser("probe", help="print a video's duration and format")
    probe_cmd.add_argument("video")
    probe_cmd.add_argument("--json", action="store_true", help="emit JSON")

    stt = sub.add_parser("transcribe",
                         help="write a timestamped transcript, for proposing chapters")
    stt.add_argument("video")
    stt.add_argument("-o", "--output", help="write here instead of standard output")
    stt.add_argument("--window", type=float, default=transcribe_mod.DEFAULT_WINDOW,
                     help=f"seconds of audio per request (default {transcribe_mod.DEFAULT_WINDOW:g}; "
                          "shorter windows lose punctuation at every cut)")
    stt.add_argument("--url", default=transcribe_mod.DEFAULT_URL,
                     help="transcription endpoint (or set VEDIT_STT_URL)")

    still = sub.add_parser("still", help="grab one frame (or take an image) and annotate it: "
                                         "highlights, dim, crop, coordinate grid")
    still.add_argument("input", help="the source video, or an image file")
    still.add_argument("spec", help="path to the JSON spec, or - to read stdin")
    still.add_argument("-o", "--output", required=True, help="the .jpg or .png to write")
    still.add_argument("--dry-run", action="store_true",
                       help="print the resolved plan and exit without rendering")
    still.add_argument("-q", "--quiet", action="store_true", help="suppress the plan")

    snip = sub.add_parser("snippet",
                          help="emit paste-ready HTML: a pinned Vidstack player with the "
                               "video's embedded chapters inlined (for LMS rich-text editors)")
    snip.add_argument("video", help="the rendered video (chapters are read from its metadata)")
    snip.add_argument("--url", required=True, help="public URL the video will be served from")
    snip.add_argument("-o", "--output", help="write here instead of standard output")
    snip.add_argument("--assets", default=snippet_mod.ASSETS_BASE,
                      help="base URL for the player assets (default: pinned jsDelivr)")
    snip.add_argument("--max-width", type=int, default=960,
                      help="player max width in px (default 960)")

    example = sub.add_parser("example", help="print an example spec")
    example.add_argument("--still", action="store_true", help="an example `still` spec instead")
    return parser


def _load_spec(path: str, info: media.MediaInfo) -> spec_mod.EditSpec:
    if path == "-":
        try:
            raw = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise VeditError(f"stdin is not valid JSON: {exc}") from None
        return spec_mod.from_dict(raw, info, origin="stdin")
    return spec_mod.load(path, info)


def _cmd_apply(args) -> int:
    info = media.probe(args.video)
    edit = _load_spec(args.spec, info)
    # Checks that need the source-to-output mapping, so --dry-run catches them too.
    render.check_timeline(edit, info)

    for line in render.plan(edit, info):
        print(line, file=sys.stderr)

    if args.dry_run:
        if not edit.slides:
            print(f"command    {render.describe_commands(edit, info)}", file=sys.stderr)
        else:
            print(f"pieces     {2 * len(edit.slides) + 1} at most "
                  f"(segments and slides, joined by ffmpeg)", file=sys.stderr)
        print("dry run: nothing was written", file=sys.stderr)
        return 0

    out = render.render(info, edit, Path(args.output), quiet=args.quiet)
    result = media.probe(out)
    print(f"wrote {out} ({result.duration:.2f}s)", file=sys.stderr)

    lines = render.chapter_lines(edit, info)
    if lines:
        print("\nchapters (paste into a description):", file=sys.stderr)
        for line in lines:
            print(f"  {line}", file=sys.stderr)

    print(out)
    return 0


def _load_still_spec(path: str, info: media.MediaInfo) -> still_mod.StillSpec:
    if path == "-":
        try:
            raw = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise VeditError(f"stdin is not valid JSON: {exc}") from None
        return still_mod.from_dict(raw, info, origin="stdin")
    return still_mod.load(path, info)


def _cmd_still(args) -> int:
    info = media.probe(args.input, still=True)
    spec = _load_still_spec(args.spec, info)
    out = Path(args.output)
    if out.suffix.lower() not in still_mod.OUTPUT_SUFFIXES:
        raise VeditError(f"write a .jpg or .png (got {out.name})")
    if out.resolve() == info.path.resolve():
        raise VeditError("refusing to write over the input; choose a different -o path")

    if not args.quiet or args.dry_run:
        for line in still_mod.plan(spec, info):
            print(line, file=sys.stderr)
    if args.dry_run:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="vedit-still-") as workdir:
            cmd = still_mod.command(spec, info, out, Path(workdir))
        print(f"command    {' '.join(cmd)}", file=sys.stderr)
        print("dry run: nothing was written", file=sys.stderr)
        return 0

    still_mod.render(info, spec, out)
    width, height = still_mod.output_size(spec, info)
    print(f"wrote {out} ({width}x{height})", file=sys.stderr)
    print(out)
    return 0


def _cmd_transcribe(args) -> int:
    info = media.probe(args.video)
    print(f"transcribing {info.duration:.0f}s in {args.window:g}s windows...",
          file=sys.stderr)
    text = transcribe_mod.transcribe(args.video, url=args.url, window=args.window)
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output}", file=sys.stderr)
        print(args.output)
    else:
        print(text)
    return 0


def _cmd_probe(args) -> int:
    info = media.probe(args.video)
    if args.json:
        print(json.dumps({
            "path": str(info.path), "duration": round(info.duration, 3),
            "fps": float(info.fps), "width": info.width, "height": info.height,
            "frames": info.total_frames, "has_audio": info.has_audio,
            "sample_rate": info.sample_rate, "channels": info.channels,
        }, indent=2))
    else:
        minutes, seconds = divmod(info.duration, 60)
        print(f"{info.path.name}")
        print(f"  duration  {info.duration:.2f}s  ({int(minutes)}:{seconds:05.2f})")
        print(f"  video     {info.width}x{info.height} @ {float(info.fps):g}fps  {info.pix_fmt}")
        print(f"  audio     {str(info.channels) + 'ch @ ' + str(info.sample_rate) + 'Hz' if info.has_audio else 'none'}")
    return 0



def _cmd_snippet(args) -> int:
    chapters = snippet_mod.read_chapters(args.video)
    if not chapters:
        print(f"note: {Path(args.video).name} has no embedded chapters; "
              "emitting a player without chapter navigation", file=sys.stderr)
    text = snippet_mod.build(args.url, chapters, assets=args.assets,
                             max_width=args.max_width)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "apply":
            return _cmd_apply(args)
        if args.command == "probe":
            return _cmd_probe(args)
        if args.command == "transcribe":
            return _cmd_transcribe(args)
        if args.command == "still":
            return _cmd_still(args)
        if args.command == "snippet":
            return _cmd_snippet(args)
        if args.command == "example":
            print(json.dumps(STILL_EXAMPLE if args.still else EXAMPLE, indent=2))
            return 0
    except VeditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # Permissions, a vanished file, an over-long command line: a message, not a traceback.
        print(f"error: {exc.strerror or exc}: {exc.filename or ''}".rstrip(": "), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
