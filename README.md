# vedit

Declarative video editing for LLM agents. You write a small JSON spec; `vedit` turns it
into a finished video — cuts, speed ramps with floating labels, captions, title slides,
chapter markers, a contents card, and loudness normalisation — using
[auto-editor](https://github.com/WyattBlue/auto-editor) and ffmpeg underneath, without
exposing either tool's command line.

It was built so that a *small local model* can edit a lecture or screencast recording
reliably: every value in the spec is validated and every error names the key that is
wrong; a dry run prints exactly what will happen and predicts the output duration; the
render is measured against that prediction (duration for a video, size for a still) and
refused if they disagree; and the source file is never written. The same repository ships the agent-facing skill that teaches a
model to use it (see [The agent skill](#the-agent-skill)).

```bash
vedit probe lecture.mp4                                   # duration, fps, resolution, audio
vedit transcribe lecture.mp4 -o transcript.txt            # "[m:ss] sentence" lines, for chapters
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4 --dry-run
vedit apply lecture.mp4 edits.json -o lecture-edited.mp4
vedit still lecture.mp4 step.json -o step-02.jpg          # one annotated frame, for a guide
vedit example            # a starter edit spec;  vedit example --still  for a still spec
```

## The edit spec

```json
{
  "cuts":     [["0:00", "1:30"], ["14:05", "end"]],
  "speed":    [{"range": ["5:00", "8:00"], "factor": 5, "label": "5x - waiting for the download"}],
  "text":     [{"range": ["9:00", "9:20"], "text": "the menu moved in v4", "position": "bottom"}],
  "slides":   [{"at": "8:00", "text": "Part 2", "seconds": 3}],
  "chapters": [{"at": "0:00", "title": "Introduction"},
               {"at": "8:00", "title": "Installing the toolchain"}],
  "toc_card": true,
  "audio":    {"normalize": "ebu", "target": -16}
}
```

| Key | What it does |
|---|---|
| `cuts` | `[start, stop]` ranges to remove. |
| `speed` | Speed a range up (`factor > 1`) or down, pitch preserved. An optional `label` floats over the sped-up stretch — the usual way to mark a skipped wait without lengthening the video. |
| `text` | Floating captions over a range; `position` is `top`, `middle` or `bottom`. |
| `slides` | Full-screen cards **inserted** at a point: `text` or an `image` file. These add to the running time. |
| `chapters` | Real chapter markers in the container, plus a pasteable `0:00 Title` list printed after the render. |
| `toc_card` | A contents card at the start, listing the chapters (`true`, or an object with `seconds`, `title`, colours). |
| `audio` | `{"normalize": "ebu"}` brings the loudness to `target` LUFS (default −16). When a plain gain would push click transients over the true-peak ceiling first, a limiter takes the clicks down (up to 12 dB); past that it stops short and the log says so. `"peak"` normalises to dBTP instead. |

Times accept `"1:30"`, `"1:02:03"`, `90` (seconds), `"90s"`, `"start"` and `"end"`.
Ranges are half-open, `[start, stop)`. **Every time refers to the original video** —
earlier cuts never shift later timestamps; `vedit` works out where things land.
Captions, slides and the contents card take `color`, `background` and `font_size`.

Every key is optional, unknown keys are rejected, and the messages say what to fix:

```
error: speed[0].factor: must be greater than 0 (got 0). Use a value above 1 to speed up, below 1 to slow down.
```

## Stills for an illustrated guide

`vedit still` grabs one frame from a video (or takes an existing image) and draws the
reader's eye to part of it — for the step-by-step guide an agent writes alongside an
instruction video:

```json
{"at": 149,
 "highlights": [{"shape": "box", "x": 1212, "y": 688, "w": 140, "h": 48, "label": "Click DOWNLOAD"}],
 "dim": 0.35, "crop": {"margin": 160}, "max_width": 1280}
```

`highlights` are box or ellipse outlines with an optional label; `dim` darkens everything
outside them; `crop` is an explicit rectangle or a `margin` around the highlights;
`max_width` downscales. `"grid": true` overlays a labelled pixel grid so a model can
*read coordinates off the frame* before marking it. Coordinates are always full-frame
pixels (or `"40%"`), even when cropping — the crop is applied last — and a highlight the
crop would remove is an error.

## Requirements

| | |
|---|---|
| **Python** | 3.12 or newer. |
| **[uv](https://docs.astral.sh/uv/)** | For installation and the test suite (`uv tool install`, `uv run pytest`). Any PEP 517 installer works for the package itself. |
| **ffmpeg / ffprobe** | On `PATH`, built with `libx264`, `libfreetype` (the `drawtext` filter) and a usable system font; `libmp3lame` as well if you use `transcribe` (it sends the server MP3). Developed and tested against 6.1.1. Used for probing, slides, captions, concatenation, loudness measurement, and every `still`. |
| **auto-editor** | 29.3.x — installed automatically as the one Python dependency, and bounded below 30 because the behaviour table in `CLAUDE.md` is version-specific. It is a shim that downloads its native binary (~40 MB) on first run, so the first render needs network access once. |
| **A speech-to-text server** (optional) | Only for `vedit transcribe`: an OpenAI-compatible `POST /v1/audio/transcriptions` endpoint that honours `response_format=verbose_json` (segment timestamps), such as [faster-whisper](https://github.com/SYSTRAN/faster-whisper) behind any of its HTTP wrappers. Point `VEDIT_STT_URL` at it, or pass `--url`. Nothing else in `vedit` needs it. |
| **[Quarto](https://quarto.org)** (optional) | Only used by the agent skill, to render the companion guide it writes (`.qmd` → self-contained HTML). |

### Install

```bash
git clone https://github.com/driegert/auto-edit.git
cd auto-edit
uv tool install --editable .        # puts `vedit` on PATH; edits to the source take effect immediately
```

## The agent skill

`skills/edit-video/SKILL.md` teaches an agent to use all of this. Given a recording and a
vague instruction ("clean this up"), the skill has the agent:

1. **survey** the video — duration, long silences and what is on screen during them,
   measured loudness — while the transcript runs in the background;
2. **offer a menu** of edits with the findings filled in (speed up the dead air, trim,
   chapters and a contents card, section slides, sound level, captions, a written
   companion, one-off exports) and ask which are wanted;
3. **write the spec**, dry-run it, show the plan;
4. **render in the background** and, meanwhile, write the companion document — a
   step-by-step guide with an annotated screenshot per step for an instruction video, a
   detailed summary for a lecture — as a Quarto `.qmd`.

It lives here, next to the code whose schema it documents, so the two cannot drift. To use
it, symlink the directory into your agent's skills folder:

```bash
ln -s "$(pwd)/skills/edit-video" ~/.claude/skills/edit-video      # Claude Code
ln -s "$(pwd)/skills/edit-video" ~/.pi/agent/skills/edit-video    # pi
```

## Why this exists

`auto-editor` does cuts and speed ramps well, but its command line has edges an agent
falls off — silently. Every row below was verified against auto-editor 29.3.1 and is
handled inside `vedit`:

| Trap | What actually happens |
|---|---|
| Omitting `--when-silent nil` | Every silent passage is removed too. |
| `--cut-out A,B C,D` | The second range is parsed as an *input filename*. |
| `--set-speed 2,20sec,25sec` | The factor comes **first**. |
| `--set-speed` over a `--cut-out` | The speed ramp wins and resurrects the cut footage. |
| `00:01:30` | Not a timecode it accepts — only `90sec`, `90s`, or frame numbers. |
| `start` / `end` / negative times | Documented on the website, rejected by the binary. |
| Two input files | They do not concatenate. Slides have to be rendered separately and joined. |
| A video with no audio | The default `--edit audio` fails outright. |

ffmpeg has a few of its own — `loudnorm`'s second pass lands about 2 dB under target,
`drawtext` expands `%{...}` even from a text file, a limiter oversampled to 192 kHz takes
the voice down with the clicks — and they are recorded in `CLAUDE.md`, together with the
reasoning behind each decision in the code.

## How it works, briefly

Slide positions split the source into spans. Each span is rendered by auto-editor with
its cuts and speed ramps (cuts are subtracted from ramps, so a ramp can never resurrect
cut footage); slides are rendered as matching clips; ffmpeg's concat demuxer joins them
with PCM audio between pieces (AAC junctions drift by ~6 ms each) and encodes audio once
at the end, in the same pass that burns in captions, attaches chapters and normalises
loudness. Captions and chapters are written in source time and mapped to output time by
the same function that predicts the final duration, so the prediction and the file agree.

Output is staged beside the destination and moved into place only when the render
succeeds and measures what the plan said it would; writing over the input is refused.

## Tests

```bash
uv run pytest            # or ./run-tests.sh
```

About 190 tests, a few minutes: they render real (synthetic) video through the actual CLI
and assert exact durations, frame counts, pixels and loudness — never "looks about right".

## License

[PolyForm Noncommercial 1.0.0](LICENSE): free to use, modify and share for personal,
educational, research and other non-commercial purposes, with attribution — keep the
copyright notice and pass the license (or its URL) along with any copy. Commercial use
needs a separate agreement; open an issue.

Copyright Dave Riegert.
