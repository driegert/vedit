"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import VeditError, __version__
from . import media, render, spec as spec_mod, transcribe as transcribe_mod

EXAMPLE = {
    "cuts": [["0:00", "1:30"], ["14:05", "end"]],
    "speed": [{"range": ["5:00", "8:00"], "factor": 5.0,
               "label": "5x - waiting for the download"}],
    "text": [{"range": ["9:00", "9:20"], "text": "note: the menu moved in v4",
              "position": "bottom"}],
    "chapters": [{"at": "0:00", "title": "Introduction"},
                 {"at": "8:00", "title": "Installing the toolchain"}],
    "toc_card": True,
    "audio": {"normalize": "ebu", "target": -16},
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
                     help="seconds per transcription window (default 120)")
    stt.add_argument("--url", default=transcribe_mod.DEFAULT_URL,
                     help="transcription endpoint (or set VEDIT_STT_URL)")

    sub.add_parser("example", help="print an example spec")
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "apply":
            return _cmd_apply(args)
        if args.command == "probe":
            return _cmd_probe(args)
        if args.command == "transcribe":
            return _cmd_transcribe(args)
        if args.command == "example":
            print(json.dumps(EXAMPLE, indent=2))
            return 0
    except VeditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
