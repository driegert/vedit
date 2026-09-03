"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

threading_event = threading.Event()   # `vedit review --serve` parks the main thread on it

from . import VeditError, __version__
from . import (ground, media, ocr, render, review as review_mod, sheet as sheet_mod,
               snippet as snippet_mod, spec as spec_mod, still as still_mod,
               transcribe as transcribe_mod)

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
    "notes": "for anything that is text, prefer {\"text\": \"Download\"} and let OCR place the "
             "box (add \"near\": [x, y] or \"occurrence\": N if it appears more than once); "
             "x/y/w/h are full-frame pixels or percentages measured with grid: true, for "
             "icons, buttons and terminal text OCR cannot read. the outline is drawn pad "
             "pixels outside the box",
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

    ocr_cmd = sub.add_parser("ocr", help="list the text on one frame with its full-frame pixel "
                                         "boxes: what a still's \"text\" anchor can name, and "
                                         "the exact on-screen spelling of things")
    ocr_cmd.add_argument("input", help="the source video, or an image file")
    ocr_cmd.add_argument("--at", help="the moment, e.g. \"2:29\" or 149 (video input only)")
    ocr_cmd.add_argument("--grep", metavar="PATTERN",
                         help="only lines matching this case-insensitive regex")

    sheet_cmd = sub.add_parser("sheet", help="a labelled contact sheet of several moments, "
                                             "to compare candidate frames in one image")
    sheet_cmd.add_argument("video")
    sheet_cmd.add_argument("times", nargs="+", help=f"up to {sheet_mod.MAX_CELLS} times, "
                                                    "e.g. 64 1:04.5 2:10")
    sheet_cmd.add_argument("-o", "--output", required=True, help="the .jpg or .png to write")
    sheet_cmd.add_argument("--cols", type=int, default=sheet_mod.DEFAULT_COLS,
                           help=f"cells per row (default {sheet_mod.DEFAULT_COLS})")
    sheet_cmd.add_argument("--width", type=int, default=sheet_mod.DEFAULT_WIDTH,
                           help=f"width of one cell in px (default {sheet_mod.DEFAULT_WIDTH})")

    rev = sub.add_parser("review", help="a click-driven review page for a guide's screenshots: "
                                        "keep, pick another moment, or draw the box; --serve "
                                        "applies each decision on the spot")
    rev.add_argument("guide", help="the .qmd guide whose images to review")
    rev.add_argument("-o", "--output", help="folder for the page and its frames "
                                            "(default: <guide>-review/ beside the guide)")
    rev.add_argument("--video", help="the source video, if the sidecars do not name it")
    rev.add_argument("--width", type=int, default=review_mod.DEFAULT_WIDTH,
                     help=f"width of the frames on the page (default {review_mod.DEFAULT_WIDTH})")
    rev.add_argument("--serve", action="store_true",
                     help="serve the page on localhost and apply decisions as they are made")
    rev.add_argument("--port", type=int, default=review_mod.DEFAULT_PORT)
    rev.add_argument("--apply", action="store_true",
                     help="apply the decisions saved in <output>/review.json and exit "
                          "(for a page that was not served)")

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


def _load_still_spec(path: str, info: media.MediaInfo) -> tuple[still_mod.StillSpec, dict]:
    """Parse the spec, returning the raw dict too (the sidecar re-serializes it verbatim)."""
    if path == "-":
        try:
            raw = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise VeditError(f"stdin is not valid JSON: {exc}") from None
        return still_mod.from_dict(raw, info, origin="stdin"), raw
    p = Path(path)
    if not p.exists():
        raise VeditError(f"spec file does not exist: {p}")
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise VeditError(f"{p.name} is not valid JSON: {exc}") from None
    return still_mod.from_dict(raw, info, origin=p.name), raw


def _cmd_still(args) -> int:
    info = media.probe(args.input, still=True)
    spec, raw_spec = _load_still_spec(args.spec, info)
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
    measured = [h for h in spec.highlights if h.text is None]
    if spec.highlights and not spec.grid:
        from dataclasses import replace
        check = out.with_name(f"{out.stem}.check{out.suffix}")
        still_mod.render(info, replace(spec, grid=still_mod.DEFAULT_GRID), check)
        if measured:
            print(f"wrote {check} -- the measuring copy: the same still with the labelled "
                  f"pixel grid over your highlights. read THIS file to verify the "
                  f"{len(measured)} box(es) you placed by x/y; embed only {out.name}.",
                  file=sys.stderr)
        else:
            print(f"wrote {check} -- the gridded copy. every box here was placed by OCR on "
                  f"its text, so you need not read it; embed only {out.name}.", file=sys.stderr)

    # The sidecar: the spec written back beside the image, with the source and output
    # recorded — and, for a text anchor, where OCR put it — so every screenshot stays
    # reproducible and tweakable. Skipped when the spec argument already *is* the sidecar
    # (re-running from one must not rewrite it mid-read); "source"/"output"/"resolved"
    # are accepted keys, so it is a valid spec.
    sidecar = out.with_name(f"{out.stem}.json")
    if args.spec == "-" or Path(args.spec).resolve() != sidecar.resolve():
        record = json.loads(json.dumps(raw_spec))
        for item, h in zip(record.get("highlights") or [], spec.highlights):
            if isinstance(item, dict) and h.resolved is not None:
                r = h.resolved
                item["resolved"] = {"x": r.x, "y": r.y, "w": r.w, "h": r.h}
        record["source"] = args.input
        record["output"] = str(out)
        sidecar.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {sidecar} -- this still's recipe (spec + source). to tweak the "
              f"shot later, edit it and re-run: vedit still {args.input} {sidecar} "
              f"-o {out}", file=sys.stderr)

    # Grounding, last so a `| tail` still shows the verdict: OCR the frame and measure
    # whether each hand-placed, labelled box covers the text its label names. Findings
    # only — the render stands either way. Text-anchored boxes were placed by OCR and
    # are only counted.
    anchored = len(spec.highlights) - len(measured)
    findings: list[ground.Finding] = []
    if any(h.label for h in measured):
        if ground.available():
            findings = ground.check(spec, info)
            for line in ground.report(findings):
                print(line, file=sys.stderr)
        elif not args.quiet:
            print("grounding  tesseract not found (or VEDIT_NO_OCR set); skipping the OCR "
                  "check of the x/y boxes", file=sys.stderr)
    if findings or anchored:
        print(ground.summary(findings, anchored), file=sys.stderr)
    print(out)
    return 0


def _cmd_ocr(args) -> int:
    info = media.probe(args.input, still=True)
    if not ocr.available():
        raise VeditError("OCR needs tesseract on PATH (and VEDIT_NO_OCR unset)")
    frame = None
    if info.is_image:
        if args.at is not None:
            raise VeditError(f"{info.path.name} is an image, so --at does not apply; drop it")
        header = f"{info.path.name}  {info.width}x{info.height}  (image)"
    else:
        if args.at is None:
            raise VeditError('needs --at: the moment to read, e.g. --at "2:29" or --at 149')
        frame, at = still_mod.frame_at(args.at, info, where="--at")
        header = f"{info.path.name}  {info.width}x{info.height}  frame {frame} at {at:.3f}s"
    rows = ocr.rows_for(info, frame)
    lines = ocr.dump(rows, args.grep)
    print(f"{header}  {len(lines)} line(s)" + (f" matching {args.grep!r}" if args.grep else "")
          + "  (tesseract, tiled 3x; boxes are full-frame pixels)", file=sys.stderr)
    for line in lines:
        print(line)
    if not lines:
        print("(nothing readable" + (" matched" if args.grep else "") + ")", file=sys.stderr)
    print("note: OCR often misses light-on-dark button captions, icons and terminal text; "
          "a control absent here may still be on screen — grab the gridded frame to see it.",
          file=sys.stderr)
    return 0


def _cmd_review(args) -> int:
    guide = Path(args.guide)
    if not guide.exists():
        raise VeditError(f"guide does not exist: {guide}")
    out_dir = Path(args.output) if args.output else guide.with_name(f"{guide.stem}-review")
    if args.apply:
        for line in review_mod.apply_all(out_dir):
            print(line, file=sys.stderr)
        print(out_dir / "review.json")
        return 0
    video = Path(args.video) if args.video else None
    page = review_mod.build(guide, out_dir, video=video, width=args.width, serve=args.serve)
    print(f"wrote {page}", file=sys.stderr)
    if not args.serve:
        print("open it in a browser; save its JSON as review.json beside it, then "
              f"`vedit review {guide} --apply`", file=sys.stderr)
        print(page)
        return 0
    server = review_mod.serve(out_dir, args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"serving {url} — decisions are applied as you make them and saved to "
          f"{out_dir / 'review.json'}; Ctrl-C to stop", file=sys.stderr)
    print(url)
    try:
        while True:
            threading_event.wait(3600)      # park the main thread; the server runs on its own
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def _cmd_sheet(args) -> int:
    info = media.probe(args.video, still=True)
    if info.is_image:
        raise VeditError("a sheet needs a video; this is an image")
    times = sheet_mod.resolve_times(args.times, info)
    out = sheet_mod.render(info, times, Path(args.output), cols=args.cols, width=args.width)
    cw, ch = sheet_mod.cell_size(info, args.width)
    print(f"wrote {out} ({len(times)} frames, {cw}x{ch} each, {args.cols} per row; each cell "
          f"is stamped with its time)", file=sys.stderr)
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
        if args.command == "ocr":
            return _cmd_ocr(args)
        if args.command == "sheet":
            return _cmd_sheet(args)
        if args.command == "review":
            return _cmd_review(args)
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
